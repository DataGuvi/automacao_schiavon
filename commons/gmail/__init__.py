"""Leitura do código OTP do Cloudflare Access via Gmail API.

O Catapult fica atrás do Cloudflare Access: ao logar, ele pede um e-mail e
manda um código de uso único para a caixa correspondente. Este módulo
autentica com a Gmail API (OAuth, token gerado por
`manutencao/gmail_oauth_setup.py`) e faz polling da caixa até achar o código.

Só leitura (`gmail.readonly`) — o módulo nunca escreve nem apaga e-mail.
"""

from __future__ import annotations

import base64
import re
import time
from datetime import datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from commons.paths import GMAIL_TOKEN_PATH

__all__ = ["fetch_otp_code", "GmailOtpError", "GmailOtpTimeout", "SCOPES"]

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Cloudflare Access manda um código numérico; 6 dígitos é o padrão observado
# nas telas de Access. Ajustar aqui se o formato real vier diferente.
_CODE_RE = re.compile(r"\b(\d{6})\b")


class GmailOtpError(RuntimeError):
    """Falha ao autenticar ou consultar a Gmail API."""


class GmailOtpTimeout(GmailOtpError):
    """Nenhum e-mail com código chegou dentro do prazo."""


def _service():
    if not GMAIL_TOKEN_PATH.exists():
        raise GmailOtpError(
            f"'{GMAIL_TOKEN_PATH}' não encontrado. Rode "
            "'python -m manutencao.gmail_oauth_setup' uma vez para autorizar."
        )

    creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        GMAIL_TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _corpo_texto(mensagem: dict) -> str:
    """Extrai o texto plano da mensagem; usa o snippet como fallback."""
    payload = mensagem.get("payload", {})
    partes = payload.get("parts") or [payload]
    for parte in partes:
        if parte.get("mimeType") == "text/plain":
            dado = parte.get("body", {}).get("data")
            if dado:
                # base64url sem padding — completa antes de decodificar.
                faltando = "=" * (-len(dado) % 4)
                return base64.urlsafe_b64decode(dado + faltando).decode(
                    "utf-8", errors="ignore")
    return mensagem.get("snippet", "")


def fetch_otp_code(
    remetente: str,
    desde: datetime,
    timeout: int = 90,
    intervalo: int = 3,
) -> str:
    """Espera e devolve o código mais recente enviado por `remetente` após `desde`.

    Faz polling porque o e-mail leva alguns segundos para chegar depois do
    Cloudflare Access disparar. `desde` evita pegar o código de uma tentativa
    de login anterior que ainda esteja na caixa.

    A query do Gmail (`q=`) usa só `from:`, sem `after:` — testado ao vivo
    contra a caixa real (spec-gmail-otp-precisao-minuto): o operador `after:`
    com epoch do Gmail só tem precisão de MINUTO (trunca os segundos), o que
    já fez o login travar esperando um código que tinha acabado de chegar. O
    corte "é desta tentativa" é feito aqui, comparando o `internalDate` (epoch
    em milissegundos, exato) de cada mensagem contra `desde`.

    Levanta `GmailOtpTimeout` se nada chegar dentro de `timeout` segundos.
    """
    service = _service()
    query = f"from:{remetente}"
    desde_ms = int(desde.timestamp() * 1000)
    prazo = time.monotonic() + timeout

    while True:
        resp = service.users().messages().list(
            userId="me", q=query, maxResults=5,
        ).execute()

        for ref in resp.get("messages", []):
            msg = service.users().messages().get(
                userId="me", id=ref["id"], format="full",
            ).execute()
            if int(msg.get("internalDate", 0)) < desde_ms:
                continue  # codigo de uma tentativa de login anterior
            codigo = _CODE_RE.search(_corpo_texto(msg))
            if codigo:
                return codigo.group(1)

        if time.monotonic() >= prazo:
            raise GmailOtpTimeout(
                f"nenhum código de '{remetente}' chegou em {timeout}s "
                f"(consultando desde {desde:%H:%M:%S})"
            )
        time.sleep(intervalo)

"""Testes de `commons.gmail.fetch_otp_code` (spec-gmail-otp-precisao-minuto)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from commons.gmail import GmailOtpTimeout, fetch_otp_code  # noqa: E402


class _Exec:
    """Fake de `.execute()`: devolve o resultado ja pronto, como a API faz."""

    def __init__(self, resultado):
        self._resultado = resultado

    def execute(self):
        return self._resultado


class _FakeMessages:
    def __init__(self, mensagens: list[dict]):
        # Mais recente primeiro, como a API devolve por padrao.
        self._mensagens = mensagens

    def list(self, userId, q, maxResults=5):  # noqa: N803 - mesma assinatura da API
        assert "after:" not in q  # R1: nunca depende do operador de data do Gmail
        return _Exec({"messages": [{"id": m["id"]} for m in self._mensagens[:maxResults]]})

    def get(self, userId, id, format):  # noqa: N803, A002
        msg = next(m for m in self._mensagens if m["id"] == id)
        return _Exec({
            "internalDate": str(msg["internal_date_ms"]),
            "snippet": msg["corpo"],
        })


class _FakeService:
    def __init__(self, mensagens: list[dict]):
        self._messages = _FakeMessages(mensagens)

    def users(self):
        return self

    def messages(self):
        return self._messages


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def test_acha_mensagem_dentro_do_mesmo_minuto_poucos_segundos_depois(monkeypatch):
    """Antes da correcao, o `after:` do Gmail (precisao de minuto) podia nao
    achar uma mensagem chegada poucos segundos depois de `desde` (achado ao
    vivo). Aqui o corte e so por `internalDate`, exato."""
    desde = datetime(2026, 10, 28, 12, 34, 50, tzinfo=timezone.utc)
    chegada = datetime(2026, 10, 28, 12, 34, 55, tzinfo=timezone.utc)  # 5s depois
    service = _FakeService([
        {"id": "1", "internal_date_ms": _ms(chegada), "corpo": "codigo: 481996"},
    ])
    monkeypatch.setattr("commons.gmail._service", lambda: service)
    assert fetch_otp_code("remetente@x.com", desde, timeout=1, intervalo=0.1) == "481996"


def test_ignora_mensagem_anterior_a_desde():
    """Codigo de uma tentativa de login anterior (mensagem mais recente na
    caixa, mas antes de `desde`) nao e usado -- GmailOtpTimeout."""
    desde = datetime(2026, 10, 28, 12, 35, 0, tzinfo=timezone.utc)
    anterior = datetime(2026, 10, 28, 12, 30, 0, tzinfo=timezone.utc)
    service = _FakeService([
        {"id": "1", "internal_date_ms": _ms(anterior), "corpo": "codigo: 111111"},
    ])
    import commons.gmail as gmail_module
    original = gmail_module._service
    gmail_module._service = lambda: service
    try:
        with pytest.raises(GmailOtpTimeout):
            fetch_otp_code("remetente@x.com", desde, timeout=0.3, intervalo=0.1)
    finally:
        gmail_module._service = original


def test_pega_a_mais_recente_entre_varias_validas(monkeypatch):
    desde = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    mais_nova = datetime(2026, 10, 28, 12, 10, 0, tzinfo=timezone.utc)
    mais_velha = datetime(2026, 10, 28, 12, 5, 0, tzinfo=timezone.utc)
    service = _FakeService([
        {"id": "nova", "internal_date_ms": _ms(mais_nova), "corpo": "codigo: 222222"},
        {"id": "velha", "internal_date_ms": _ms(mais_velha), "corpo": "codigo: 333333"},
    ])
    monkeypatch.setattr("commons.gmail._service", lambda: service)
    assert fetch_otp_code("remetente@x.com", desde, timeout=1, intervalo=0.1) == "222222"

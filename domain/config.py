"""Configuração tipada do RPA.

Os fluxos recebem UM objeto `Config`, não um `dict` cru com `env.get("CHAVE")`
espalhado. Os nomes de chave do profile ficam todos aqui, num lugar só: quem
precisa da credencial do ERP escreve `config.ecrs.usuario`, e um typo vira
`AttributeError` na hora, não um `None` que só aparece no login.

Profile por ambiente: `resources/config-dev.env` e `resources/config-prod.env`
(`commons/paths.profile_path`). O ativo sai da variável de ambiente `RPA_ENV`,
default `prod`. Não existe arquivo genérico.

Só a conexão com o banco é obrigatória para carregar: sem ela nenhum fluxo tem
o que fazer. Credencial de sistema externo ausente NÃO derruba o carregamento —
cada fluxo já sabe pular o seu sistema com aviso, e `Config.checar_sistema`
alimenta o `dim_sistema` com o que falta (ver `domain/service/sistema_service`).

Segredos (`senha`, tokens) ficam fora do `repr`, para não vazar em log nem em
traceback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from commons.db import ConfigBanco, load_env
from commons.email_client import ConfigSmtp
from commons.exception import ConfigException
from commons.messaging.twilio_config import ConfigTwilio
from commons.paths import AMBIENTE_PADRAO, AMBIENTES, profile_path
from domain.sistemas import Sistema

__all__ = [
    "Config", "ConfigEcrs", "ConfigSmtp", "ConfigTwilio", "ConfigVision",
    "Credencial", "DESTINATARIOS_CLIENTE", "carregar_config",
]

# Quem recebe os relatorios .docx da conciliacao (nao e segredo). BPO ligado em 2026-10-02.
DESTINATARIOS_CLIENTE = (
    "bpo@rokkasmarket.com",
    "cauet.menezes@dataguvi.com.br",
)

_VERDADEIRO = frozenset({"1", "true", "t", "yes", "y", "sim", "s"})
_FALSO = frozenset({"0", "false", "f", "no", "n", "nao", "não"})

# Chaves sem as quais nao ha conexao. Conferidas juntas, para a mensagem dizer
# TODAS as que faltam de uma vez.
_CHAVES_BANCO = ("HOST", "PORT", "DATABASE", "USER_GUVI", "PASSWORD_GUVI")


@dataclass(frozen=True)
class Credencial:
    usuario: str
    senha: str = field(repr=False)


@dataclass(frozen=True)
class ConfigVision:
    api_key: str = field(repr=False)
    modelo: str


@dataclass(frozen=True)
class ConfigEcrs:
    """ERP Catapult/ECRS: credencial, e-mail do Cloudflare Access e URL por loja."""

    usuario: str
    senha: str = field(repr=False)
    access_email: str
    headless: bool
    url_hq: str
    url_windermere: str
    url_drphilips: str

    def url_da_loja(self, id_loja: int) -> str:
        """URL do Catapult da loja (`dim_loja.id`). Vazia se a loja nao tem.

        HQ nao tem invoice de loja, por isso nao entra neste de-para.
        """
        return {1: self.url_windermere, 2: self.url_drphilips}.get(id_loja, "")


@dataclass(frozen=True)
class Config:
    ambiente: str
    banco: ConfigBanco
    # SharePoint das invoices (FLUXO 2) e da cotacao (FLUXO 3, outra conta).
    sharepoint: Credencial
    sharepoint_cotacao: Credencial
    vision: ConfigVision
    ecrs: ConfigEcrs
    smtp: ConfigSmtp
    twilio: ConfigTwilio
    alerta_email: str
    sinonimos_sheet_id: str

    def checar_sistema(self, sistema: Sistema) -> tuple[bool, str | None]:
        """Proxy barato de "dá para autenticar?": credenciais do sistema presentes.

        Nao faz chamada de rede — o `acesso_ok` real vem das falhas de login
        dentro dos fluxos. Retorna (ok, mensagem_ou_None); a mensagem nomeia as
        chaves do profile que faltam, nunca os valores.
        """
        faltando = [chave for chave, valor in self._exigidos(sistema) if not valor]
        if faltando:
            return False, f"profile sem: {', '.join(faltando)}"
        return True, None

    def _exigidos(self, sistema: Sistema) -> tuple[tuple[str, str], ...]:
        """(chave do profile, valor) que cada sistema precisa para autenticar."""
        sp, sp2 = self.sharepoint, self.sharepoint_cotacao
        tw = self.twilio
        # A credencial de fato do Sheets e o arquivo de Service Account, nao uma
        # chave do profile: `SINONIMOS_SHEET_ID` e o proxy barato de "a planilha
        # esta configurada" que da para enxergar daqui.
        tabela = {
            Sistema.SHAREPOINT_WINDERMERE: (
                ("SHAREPOINT_USERNAME", sp.usuario), ("SHAREPOINT_PASSWORD", sp.senha)),
            Sistema.SHAREPOINT_DRPHILLIPS: (
                ("SHAREPOINT_USERNAME", sp.usuario), ("SHAREPOINT_PASSWORD", sp.senha)),
            Sistema.TWILIO_WHATSAPP: (
                ("ACCOUNT_SID", tw.account_sid), ("AUTH_TOKEN", tw.auth_token),
                ("TWILIO_NUMBER", tw.numero), ("TWILIO_CONTENT_SID", tw.content_sid)),
            Sistema.SMTP_EMAIL: (
                ("SMTP_HOST", self.smtp.host), ("SMTP_PORT", self.smtp.port),
                ("SMTP_USER", self.smtp.usuario), ("SMTP_PASSWORD", self.smtp.senha),
                ("SMTP_FROM", self.smtp.remetente)),
            Sistema.ANTHROPIC_VISION: (("schiavon_key_vision", self.vision.api_key),),
            Sistema.ERP_CATAPULT: (
                ("ECRS_USER", self.ecrs.usuario), ("ECRS_PASSWORD", self.ecrs.senha)),
            Sistema.GOOGLE_SHEETS: (("SINONIMOS_SHEET_ID", self.sinonimos_sheet_id),),
        }
        return tabela[sistema]

    @classmethod
    def de_valores(cls, valores: dict[str, str], ambiente: str = AMBIENTE_PADRAO) -> Config:
        """Monta a config a partir de `chave -> valor` já lido do profile.

        Separado de `carregar_config` para o teste montar uma config sem
        arquivo. Levanta `ConfigException` listando TODAS as chaves de conexão
        ausentes.
        """
        def txt(chave: str, padrao: str = "") -> str:
            return (valores.get(chave) or "").strip() or padrao

        faltando = [k for k in _CHAVES_BANCO if not txt(k)]
        if faltando:
            raise ConfigException(
                f"profile config-{ambiente}.env sem as chaves de conexao: "
                f"{', '.join(faltando)}."
            )

        return cls(
            ambiente=ambiente,
            banco=ConfigBanco(
                host=txt("HOST"), port=txt("PORT"), database=txt("DATABASE"),
                user=txt("USER_GUVI"), password=valores["PASSWORD_GUVI"],
                schema=txt("SCHEMA", "public"),
            ),
            sharepoint=Credencial(txt("SHAREPOINT_USERNAME"), txt("SHAREPOINT_PASSWORD")),
            sharepoint_cotacao=Credencial(
                txt("SHAREPOINT_USERNAME2"), txt("SHAREPOINT_PASSWORD2")),
            vision=ConfigVision(
                api_key=txt("schiavon_key_vision"),
                modelo=txt("VISION_MODEL", "claude-sonnet-4-6"),
            ),
            #Configuração do ERP
            ecrs=ConfigEcrs(
                usuario=txt("ECRS_USER"), senha=txt("ECRS_PASSWORD"),
                access_email=txt("CLOUDFLARE_ACCESS_EMAIL"),
                headless=_booleano(valores, "ECRS_HEADLESS", True),
                url_hq=txt("ECRS_HQ"), url_windermere=txt("ECRS_WINDERMERE"),
                url_drphilips=txt("ECRS_DRPHILIPS"),
            ),
            smtp=ConfigSmtp(
                host=txt("SMTP_HOST"), port=txt("SMTP_PORT"), usuario=txt("SMTP_USER"),
                senha=txt("SMTP_PASSWORD"), remetente=txt("SMTP_FROM"),
            ),
            twilio=ConfigTwilio(
                account_sid=txt("ACCOUNT_SID"), auth_token=txt("AUTH_TOKEN"),
                numero=txt("TWILIO_NUMBER"), content_sid=txt("TWILIO_CONTENT_SID"),
            ),
            alerta_email=txt("ALERTA_EMAIL"),
            sinonimos_sheet_id=txt("SINONIMOS_SHEET_ID"),
        )


def _booleano(valores: dict[str, str], chave: str, padrao: bool) -> bool:
    """Le uma chave booleana. Ausente ou vazia devolve `padrao`.

    Existe porque o profile devolve tudo como string, e string nao-vazia e
    sempre verdadeira em Python: `bool(valores.get("X"))` num `X=false` da True.

    Valor preenchido mas irreconhecivel e `ConfigException`, nao o padrao
    silencioso: um `ECRS_HEADLESS=falso` (que nao esta na lista) seria um robo
    rodando no modo oposto ao que quem editou o profile pediu, sem aviso.
    """
    bruto = (valores.get(chave) or "").strip().lower()
    if not bruto:
        return padrao
    if bruto in _VERDADEIRO:
        return True
    if bruto in _FALSO:
        return False
    raise ConfigException(
        f"{chave}={bruto!r} nao e um booleano reconhecido no profile; "
        f"use um de {sorted(_VERDADEIRO)} ou {sorted(_FALSO)}."
    )


def carregar_config(ambiente: str | None = None) -> Config:
    """Carrega o profile do ambiente ativo e devolve a `Config`.

    `ambiente` explícito vence; senão `RPA_ENV`; senão `prod`. Ambiente fora de
    `dev`/`prod` é `ConfigException` — um `RPA_ENV=prd` que caísse em silêncio
    no default apontaria o robô para o banco errado.
    """
    escolhido = (ambiente or os.environ.get("RPA_ENV") or AMBIENTE_PADRAO).strip().lower()
    if escolhido not in AMBIENTES:
        raise ConfigException(
            f"RPA_ENV={escolhido!r} invalido; use um de {list(AMBIENTES)}."
        )
    return Config.de_valores(load_env(profile_path(escolhido)), escolhido)

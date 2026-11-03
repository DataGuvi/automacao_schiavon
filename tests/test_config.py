"""Configuração tipada: parser estrito do profile, validação e escolha do ambiente.

Nada aqui toca o profile real nem o banco: o profile é um arquivo em `tmp_path`
e a `Config` é montada por `Config.de_valores`.
"""

from __future__ import annotations

import pytest

from commons.db import ConfigBanco, load_env
from commons.exception import ConfigException
from domain.config import Config, carregar_config
from domain.sistemas import Sistema

BANCO = {"HOST": "db.local", "PORT": "5432", "DATABASE": "rpa", "USER_GUVI": "u", "PASSWORD_GUVI": "s3nh4"}


def _cfg(**extra: str) -> Config:
    return Config.de_valores({**BANCO, **extra})


# --------------------------------------------------------------------------
# load_env: parser estrito
# --------------------------------------------------------------------------

def test_load_env_le_chave_valor_e_ignora_comentario(tmp_path):
    f = tmp_path / "p.env"
    f.write_text("# comentario\n\nHOST=db.local\nPORT=5432\n", encoding="utf-8")
    assert load_env(f) == {"HOST": "db.local", "PORT": "5432"}


def test_load_env_mantem_dois_pontos_dentro_do_valor(tmp_path):
    # o parser antigo cortava no primeiro ':' quando a linha nao tinha '='
    f = tmp_path / "p.env"
    f.write_text("ECRS_HQ=https://catapult.exemplo:8443/app\n", encoding="utf-8")
    assert load_env(f)["ECRS_HQ"] == "https://catapult.exemplo:8443/app"


def test_load_env_rejeita_separador_dois_pontos(tmp_path):
    f = tmp_path / "p.env"
    f.write_text("HOST=ok\nPASSWORD: segredo\n", encoding="utf-8")
    with pytest.raises(ConfigException) as exc:
        load_env(f)
    # diz a LINHA, nunca o conteudo (que pode ser segredo)
    assert "[2]" in str(exc.value)
    assert "segredo" not in str(exc.value)


def test_load_env_nao_interpola_variavel_na_senha(tmp_path):
    f = tmp_path / "p.env"
    f.write_text("PASSWORD=ab${HOME}cd\n", encoding="utf-8")
    assert load_env(f)["PASSWORD"] == "ab${HOME}cd"


def test_load_env_arquivo_ausente_e_config_exception(tmp_path):
    with pytest.raises(ConfigException) as exc:
        load_env(tmp_path / "nao-existe.env")
    assert "nao-existe.env" in str(exc.value)


# --------------------------------------------------------------------------
# Config.de_valores
# --------------------------------------------------------------------------

def test_chaves_de_banco_faltando_sao_listadas_de_uma_vez():
    with pytest.raises(ConfigException) as exc:
        Config.de_valores({"HOST": "db.local", "PORT": "5432"})
    msg = str(exc.value)
    assert "DATABASE" in msg and "USER" in msg and "PASSWORD" in msg
    assert "HOST" not in msg


def test_chave_de_banco_so_com_espacos_conta_como_faltando():
    with pytest.raises(ConfigException):
        Config.de_valores({**BANCO, "USER_GUVI": "   "})


def test_config_minima_carrega_com_defaults():
    c = _cfg()
    assert c.banco == ConfigBanco("db.local", "5432", "rpa", "u", "s3nh4", "public")
    assert c.vision.modelo == "claude-sonnet-4-6"
    assert c.ecrs.headless is True
    assert c.sinonimos_sheet_id == ""
    assert c.conferencia_manual_sheet_id == ""
    assert c.alerta_email == ""


def test_schema_e_modelo_vem_do_profile():
    c = _cfg(SCHEMA="dwschiavon2", VISION_MODEL="outro-modelo")
    assert c.banco.schema == "dwschiavon2"
    assert c.vision.modelo == "outro-modelo"


@pytest.mark.parametrize("valor,esperado", [
    ("false", False), ("FALSE", False), ("0", False), ("nao", False),
    ("true", True), ("1", True), ("sim", True), ("", True),
])
def test_ecrs_headless_converte_texto_em_booleano(valor, esperado):
    assert _cfg(ECRS_HEADLESS=valor).ecrs.headless is esperado


def test_ecrs_headless_irreconhecivel_nao_cai_no_default_em_silencio():
    with pytest.raises(ConfigException) as exc:
        _cfg(ECRS_HEADLESS="falso")
    assert "ECRS_HEADLESS" in str(exc.value)


def test_url_da_loja_por_id():
    c = _cfg(ECRS_WINDERMERE="https://w", ECRS_DRPHILIPS="https://d", ECRS_HQ="https://h")
    assert c.ecrs.url_da_loja(1) == "https://w"
    assert c.ecrs.url_da_loja(2) == "https://d"
    assert c.ecrs.url_da_loja(99) == ""  # HQ nao tem invoice de loja


def test_segredos_ficam_fora_do_repr():
    c = _cfg(ECRS_PASSWORD="segredo-erp", AUTH_TOKEN="tok-twilio",
             SMTP_PASSWORD="segredo-smtp", schiavon_key_vision="sk-vision",
             SHAREPOINT_PASSWORD="segredo-sp")
    texto = repr(c)
    for segredo in ("s3nh4", "segredo-erp", "tok-twilio", "segredo-smtp",
                    "sk-vision", "segredo-sp"):
        assert segredo not in texto


# --------------------------------------------------------------------------
# Config.checar_sistema
# --------------------------------------------------------------------------

def test_checar_sistema_aponta_chaves_ausentes_sem_vazar_valores():
    ok, msg = _cfg(ECRS_USER="usuario").checar_sistema(Sistema.ERP_CATAPULT)
    assert ok is False
    assert "ECRS_PASSWORD" in msg and "ECRS_USER" not in msg


def test_checar_sistema_ok_quando_credenciais_presentes():
    c = _cfg(ECRS_USER="u", ECRS_PASSWORD="p")
    assert c.checar_sistema(Sistema.ERP_CATAPULT) == (True, None)


def test_todo_sistema_tem_regra_de_checagem():
    c = _cfg()
    for sistema in Sistema:
        ok, msg = c.checar_sistema(sistema)  # nao levanta KeyError
        assert ok is False and msg


# --------------------------------------------------------------------------
# carregar_config: escolha do ambiente
# --------------------------------------------------------------------------

def _profile(tmp_path, monkeypatch, **arquivos: str):
    """Aponta `profile_path` para `tmp_path`, com um arquivo por ambiente."""
    for ambiente, host in arquivos.items():
        corpo = "\n".join(f"{k}={v}" for k, v in {**BANCO, "HOST": host}.items())
        (tmp_path / f"config-{ambiente}.env").write_text(corpo, encoding="utf-8")
    monkeypatch.setattr("domain.config.profile_path", lambda a: tmp_path / f"config-{a}.env")


def test_sem_rpa_env_usa_prod(tmp_path, monkeypatch):
    monkeypatch.delenv("RPA_ENV", raising=False)
    _profile(tmp_path, monkeypatch, dev="host-dev", prod="host-prod")
    c = carregar_config()
    assert (c.ambiente, c.banco.host) == ("prod", "host-prod")


def test_rpa_env_dev_carrega_o_profile_dev(tmp_path, monkeypatch):
    monkeypatch.setenv("RPA_ENV", "dev")
    _profile(tmp_path, monkeypatch, dev="host-dev", prod="host-prod")
    assert carregar_config().banco.host == "host-dev"


def test_argumento_explicito_vence_rpa_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RPA_ENV", "dev")
    _profile(tmp_path, monkeypatch, dev="host-dev", prod="host-prod")
    assert carregar_config("prod").banco.host == "host-prod"


def test_rpa_env_invalido_nao_cai_em_prod_em_silencio(tmp_path, monkeypatch):
    monkeypatch.setenv("RPA_ENV", "prd")
    _profile(tmp_path, monkeypatch, prod="host-prod")
    with pytest.raises(ConfigException) as exc:
        carregar_config()
    assert "prd" in str(exc.value)


def test_profile_do_ambiente_ausente_e_config_exception(tmp_path, monkeypatch):
    monkeypatch.setenv("RPA_ENV", "dev")
    _profile(tmp_path, monkeypatch, prod="host-prod")  # so existe o prod
    with pytest.raises(ConfigException) as exc:
        carregar_config()
    assert "config-dev.env" in str(exc.value)

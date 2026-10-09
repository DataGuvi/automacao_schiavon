"""Acesso manual ao SharePoint de uma loja (somente leitura).

    python -m manutencao.sharepoint_loja                # Dr. Phillips: lista as 2 semanas
    python -m manutencao.sharepoint_loja --loja 1       # Windermere
    python -m manutencao.sharepoint_loja --janela       # abre o navegador logado (precisa de tela)

Nao baixa, nao grava no banco, nao registra monitor.
Spec: .claude/rules/spec-sharepoint-loja.md.
"""

from __future__ import annotations

import argparse
from datetime import date
from urllib.parse import urlparse

from commons import sharepoint as sp
from commons.catapult import fechar_browser, parar_playwright
from commons.datas import fixar_fuso
from commons.db import conexao
from commons.exception import BusinessException
from commons.logging_config import configurar_logs, get_logger
from domain.config import carregar_config
from domain.service.invoice_service import fetch_all_configs

log = get_logger(__name__)


def _registro_da_loja(config, id_loja: int) -> dict:
    with conexao(config.banco) as conn:
        registros = fetch_all_configs(conn)
    for registro in registros:
        if registro["id"] == id_loja:
            return registro
    raise BusinessException(f"loja id={id_loja} sem configuracao de SharePoint em dim_loja")


def _listar_semana(context, registro: dict, referencia: date) -> None:
    """Lista a pasta da semana; pasta inexistente vira aviso, nao erro."""
    site_url, raiz = sp.parse_sharepoint_url(registro["url"])
    url = urlparse(registro["url"])
    base = f"{url.scheme}://{url.netloc}"
    log.info("=== semana de %s (pasta %s) ===", referencia.strftime("%d/%m/%Y"),
             sp.nome_pasta_semana(referencia))
    try:
        caminho, entradas = sp.navigate(context, site_url, base, raiz, sp.build_nav_steps(referencia))
    except RuntimeError as exc:
        log.warning("%s", exc)
        return
    log.info("Pasta: %s", caminho)
    if not entradas:
        log.info("(vazia)")
    for e in entradas:
        if e["type"] == "pasta":
            log.info("[pasta  ] %s (%s itens)", e["name"], e.get("item_count", "?"))
        else:
            log.info("[arquivo] %s (%s B)", e["name"], e.get("size_bytes", "?"))


def _usar_sessao(registro: dict, janela: bool, context, page) -> None:
    if janela:
        page.goto(registro["url"], wait_until="domcontentloaded", timeout=60_000)
        input("Navegador aberto na pasta da loja. Pressione Enter para fechar...")
        return
    for referencia in sp.semanas_da_coleta(date.today()):
        _listar_semana(context, registro, referencia)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--loja", type=int, default=2, help="id da loja (1 Windermere, 2 Dr. Phillips)")
    parser.add_argument("--janela", action="store_true", help="abre o navegador visivel e logado")
    args = parser.parse_args()

    configurar_logs()
    fixar_fuso()
    config = carregar_config()
    registro = _registro_da_loja(config, args.loja)
    log.info("Loja %s - %s", registro["id"], registro["name"])

    cred = config.sharepoint
    pw, browser, context, page = sp.open_sharepoint_session(
        cred.usuario, cred.senha, registro["url"], headless=False)

    _usar_sessao(registro, args.janela, context, page)


if __name__ == "__main__":
    main()

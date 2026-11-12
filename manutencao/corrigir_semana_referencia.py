"""Corrige `fat_invoice.semana_referencia` pela pasta do SharePoint onde cada arquivo esta.

    python -m manutencao.corrigir_semana_referencia            # simula: so lista o que muda
    python -m manutencao.corrigir_semana_referencia --aplicar  # grava no banco

Le (sem baixar nem mover nada) a pasta de cada semana desde o inicio da coleta, com as
subpastas, e casa o nome original com `fat_invoice.arquivo`. Spec: spec-painel-semana-pasta R5.
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from datetime import date, timedelta
from urllib.parse import urlparse

from commons import sharepoint as sp
from commons.catapult import fechar_browser, parar_playwright
from commons.datas import fixar_fuso, week_bounds
from commons.db import conexao
from commons.logging_config import configurar_logs, get_logger
from crawler.flow.invoices_flow import INICIO_COLETA
from domain.config import carregar_config
from domain.service.invoice_service import (
    atualizar_semana_referencia, fetch_all_configs, fetch_semanas_invoice,
)

log = get_logger(__name__)

# '<prefixo>_<nome original>_<dd-mm-aaaa>.<ext>' (commons.sharepoint._build_filename)
_NOME_BAIXADO = re.compile(r"^[^_]+_(.+)_\d{2}-\d{2}-\d{4}$")


def _nome_original(arquivo: str) -> str:
    stem = arquivo.rsplit(".", 1)[0]
    m = _NOME_BAIXADO.match(stem)
    return (m.group(1) if m else stem).lower()


def _arquivos(context, site_url: str, base: str, pasta: str) -> list[str]:
    """Nomes (sem extensao) dos arquivos da pasta e de todas as subpastas."""
    nomes = []
    for e in sp.get_folder_contents(context, site_url, pasta, base):
        if e["type"] == "arquivo":
            nomes.append(e["name"].rsplit(".", 1)[0].lower())
        else:
            nomes += _arquivos(context, site_url, base, e["server_relative_url"])
    return nomes


def _pasta_da_semana(entries: list[dict], segunda: date) -> dict | None:
    """'DD A DD' comecando na segunda, aceitando sufixo ('12 A 18 PASTA FECHADA')."""
    for e in entries:
        m = re.match(r"^(\d{1,2})\s*[Aa-]\s*\d{1,2}\b", e["name"].strip())
        if e["type"] == "pasta" and m and int(m.group(1)) == segunda.day:
            return e
    return None


def _arquivos_da_semana(context, registro: dict, segunda: date) -> list[str]:
    """Arquivos da pasta da semana; pasta inexistente devolve lista vazia."""
    site_url, raiz = sp.parse_sharepoint_url(registro["url"])
    u = urlparse(registro["url"])
    base = f"{u.scheme}://{u.netloc}"
    try:
        passos = sp.build_nav_steps(segunda)[:-1] + [lambda e: _pasta_da_semana(e, segunda)]
        pasta, _ = sp.navigate(context, site_url, base, raiz, passos)
        return _arquivos(context, site_url, base, pasta)
    except RuntimeError as exc:
        log.warning("loja %s semana %s: %s", registro["id"], segunda, exc)
        return []


def _mapa_sharepoint(context, page, registros: list[dict]) -> dict[tuple[int, str], set[date]]:
    """(id_loja, nome original) -> semanas em cuja pasta o arquivo aparece."""
    atual, _ = week_bounds(date.today())
    mapa: dict[tuple[int, str], set[date]] = defaultdict(set)
    for registro in registros:
        page.goto(registro["url"], wait_until="domcontentloaded", timeout=60_000)
        segunda = INICIO_COLETA
        while segunda <= atual:
            nomes = _arquivos_da_semana(context, registro, segunda)
            log.info("loja %s semana %s: %s arquivo(s)", registro["id"], segunda, len(nomes))
            for nome in nomes:
                mapa[(registro["id"], nome)].add(segunda)
            segunda += timedelta(days=7)
    return mapa


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--aplicar", action="store_true", help="grava no banco (default: simula)")
    args = parser.parse_args()

    configurar_logs()
    fixar_fuso()
    config = carregar_config()
    with conexao(config.banco) as conn:
        registros = fetch_all_configs(conn)
        notas = fetch_semanas_invoice(conn)

    cred = config.sharepoint
    pw, browser, context, page = sp.open_sharepoint_session(
        cred.usuario, cred.senha, registros[0]["url"], headless=True)
    try:
        mapa = _mapa_sharepoint(context, page, registros)
    finally:
        fechar_browser(browser)
        parar_playwright(pw)

    mudar, sem_pasta, ambiguas = {}, [], []
    for n in notas:
        semanas = mapa.get((n["id_loja"], _nome_original(n["arquivo"] or "")), set())
        if not semanas:
            sem_pasta.append(n)
        elif len(semanas) > 1:
            ambiguas.append((n, sorted(semanas)))
        elif (semana := next(iter(semanas))) != n["semana_referencia"]:
            mudar[n["id"]] = semana
            print("id=%s loja=%s %s: %s -> %s" % (
                n["id"], n["id_loja"], n["arquivo"], n["semana_referencia"], semana))

    for n, semanas in ambiguas:
        print("AMBIGUA id=%s %s em %s (nao muda)" % (n["id"], n["arquivo"], semanas))
    for n in sem_pasta:
        print("SEM PASTA id=%s loja=%s %s (nao muda, fica %s)" % (
            n["id"], n["id_loja"], n["arquivo"], n["semana_referencia"]))
    print("\n%s nota(s): %s a corrigir, %s ambigua(s), %s sem pasta." % (
        len(notas), len(mudar), len(ambiguas), len(sem_pasta)))

    if not args.aplicar:
        print("Simulacao - nada gravado. Rode com --aplicar para gravar.")
        return
    with conexao(config.banco) as conn:
        atualizar_semana_referencia(conn, mudar)
    print("Gravado.")


if __name__ == "__main__":
    main()

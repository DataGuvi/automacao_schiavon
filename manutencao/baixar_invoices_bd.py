"""Baixa do SharePoint os arquivos das notas ja gravadas no BD (so leitura no banco).

Salva em `files/unprocessed_files` como `rep_<id>__<nome original>`, sem prefixo de loja, para o
robo nao relê-los. Grava o manifesto `files/reprocesso_insumo.json` que `estatistica_insumo` usa.
Retomavel: arquivo ja baixado e pulado. Spec: .claude/rules/spec-reprocesso-insumo.md.

    python -m manutencao.baixar_invoices_bd
    python -m manutencao.baixar_invoices_bd --janela      # navegador visivel
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

from commons import sharepoint as sp
from commons.catapult import fechar_browser, parar_playwright
from commons.datas import fixar_fuso, week_bounds
from commons.db import conexao
from commons.logging_config import configurar_logs, get_logger
from commons.paths import DOWNLOAD_DIR, FILES_DIR
from domain.config import carregar_config
from domain.service.invoice_service import fetch_all_configs, fetch_invoices_para_reprocesso

log = get_logger(__name__)

MANIFESTO = FILES_DIR / "reprocesso_insumo.json"
PREFIXO_REP = "rep_"

_ARQUIVO_RE = re.compile(
    r"^(?:wind|drphil)_(?P<stem>.+)_(?P<data>\d{2}-\d{2}-\d{4})(?P<ext>\.[^.]+)$", re.IGNORECASE,
)
_EMISSAO_PLAUSIVEL = timedelta(days=120)


def nome_original(arquivo: str) -> tuple[str, date] | None:
    """`(nome no SharePoint, data do download)` de um `fat_invoice.arquivo`, ou None."""
    m = _ARQUIVO_RE.match(arquivo or "")
    if not m:
        return None
    dia, mes, ano = (int(x) for x in m["data"].split("-"))
    try:
        baixado = date(ano, mes, dia)
    except ValueError:
        return None
    return m["stem"] + m["ext"], baixado


def semanas_candidatas(baixado: date, dt_emissao: date | None) -> list[date]:
    """Segundas-feiras onde o arquivo pode estar: a do download, a anterior e a da emissao."""
    segunda = week_bounds(baixado)[0]
    candidatas = [segunda, segunda - timedelta(days=7)]
    if dt_emissao and abs(baixado - dt_emissao) <= _EMISSAO_PLAUSIVEL:
        candidatas.append(week_bounds(dt_emissao)[0])
    return list(dict.fromkeys(candidatas))


def _listar_ou_vazio(context, site_url: str, caminho: str, base: str) -> list[dict]:
    try:
        return sp.get_folder_contents(context, site_url, caminho, base)
    except RuntimeError:
        log.warning("pasta ilegivel: %s", caminho)
        return []


def _indexar_semana(context, registro: dict, segunda: date) -> dict[str, str]:
    """`nome minusculo -> caminho no SharePoint` da pasta da semana e de uma subpasta abaixo."""
    site_url, raiz = sp.parse_sharepoint_url(registro["url"])
    url = urlparse(registro["url"])
    base = f"{url.scheme}://{url.netloc}"
    try:
        caminho, entradas = sp.navigate(context, site_url, base, raiz, sp.build_nav_steps(segunda))
    except RuntimeError:
        log.warning("loja %s: semana de %s nao encontrada", registro["id"], segunda)
        return {}
    indice: dict[str, str] = {}
    for entrada in entradas:
        if entrada["type"] == "pasta":
            for sub in _listar_ou_vazio(context, site_url, entrada["server_relative_url"], base):
                if sub["type"] == "arquivo":
                    indice.setdefault(sub["name"].lower(), sub["server_relative_url"])
        else:
            indice.setdefault(entrada["name"].lower(), entrada["server_relative_url"])
    return indice


def _achar(indices: list[dict[str, str]], nome: str) -> str | None:
    """Caminho do arquivo nos indices das semanas candidatas; casa pelo nome e, na falta, pelo stem."""
    alvo = nome.lower()
    stem = Path(alvo).stem
    for indice in indices:
        if alvo in indice:
            return indice[alvo]
    for indice in indices:
        for achado, caminho in indice.items():
            if Path(achado).stem == stem:
                return caminho
    return None


def _baixar(context, site_url: str, caminho: str, destino: Path) -> bool:
    try:
        sp.download_single_file(context, site_url, caminho, destino)
        return True
    except RuntimeError:
        log.exception("falha baixando %s", caminho)
        return False


def _referencia(nota: dict) -> dict:
    return {
        "cod_status_conc": nota["cod_status_conc"],
        "anotacao_geral": nota["anotacao_manual_geral"],
        "fornecedor": nota["nome_fornecedor"],
        "numero": nota["numero_invoice"],
    }


def _processar_nota(context, registro: dict, nota: dict, cache: dict) -> dict:
    """Acha e baixa o arquivo de uma nota; devolve a linha do manifesto."""
    linha = {"id": nota["id"], "id_loja": nota["id_loja"], "arquivo_bd": nota["arquivo"],
             "baixado": None, "motivo": None, "ref": _referencia(nota)}
    partes = nome_original(nota["arquivo"])
    if partes is None:
        linha["motivo"] = "nome fora do padrao"
        return linha
    original, baixado_em = partes
    indices = []
    for segunda in semanas_candidatas(baixado_em, nota["dt_emissao"]):
        chave = (registro["id"], segunda)
        if chave not in cache:
            cache[chave] = _indexar_semana(context, registro, segunda)
        indices.append(cache[chave])
    caminho = _achar(indices, original)
    if caminho is None:
        linha["motivo"] = "nao achado no SharePoint"
        return linha
    destino = DOWNLOAD_DIR / f"{PREFIXO_REP}{nota['id']}__{original}"
    site_url, _ = sp.parse_sharepoint_url(registro["url"])
    if destino.exists() or _baixar(context, site_url, caminho, destino):
        linha["baixado"] = destino.name
    else:
        linha["motivo"] = "falha no download"
    return linha


def _fechar_sessao(pw, browser) -> None:
    fechar_browser(browser)
    parar_playwright(pw)


def _baixar_loja(config, registro: dict, notas: list[dict], janela: bool) -> list[dict]:
    cred = config.sharepoint
    pw, browser, context, _page = sp.open_sharepoint_session(
        cred.usuario, cred.senha, registro["url"], headless=not janela)
    try:
        cache: dict = {}
        linhas = [_processar_nota(context, registro, n, cache) for n in notas]
    finally:
        _fechar_sessao(pw, browser)
    achadas = sum(1 for linha in linhas if linha["baixado"])
    log.info("loja %s: %s/%s arquivo(s) baixado(s)", registro["id"], achadas, len(linhas))
    return linhas


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--janela", action="store_true", help="navegador visivel")
    args = parser.parse_args()

    configurar_logs()
    fixar_fuso()
    config = carregar_config()
    with conexao(config.banco) as conn:
        registros = {r["id"]: r for r in fetch_all_configs(conn)}
        notas = fetch_invoices_para_reprocesso(conn)
    log.info("%s nota(s) no BD", len(notas))

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    manifesto: list[dict] = []
    for id_loja, registro in registros.items():
        da_loja = [n for n in notas if n["id_loja"] == id_loja]
        if da_loja:
            manifesto += _baixar_loja(config, registro, da_loja, args.janela)

    MANIFESTO.write_text(json.dumps(manifesto, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    sem_arquivo = [m for m in manifesto if not m["baixado"]]
    log.info("manifesto: %s (%s nota(s), %s sem arquivo)", MANIFESTO, len(manifesto), len(sem_arquivo))
    for m in sem_arquivo:
        log.warning("sem arquivo: id=%s %s (%s)", m["id"], m["arquivo_bd"], m["motivo"])


if __name__ == "__main__":
    main()

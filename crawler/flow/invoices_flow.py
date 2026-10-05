"""FLUXO 2 — Coleta e leitura de invoices.

SharePoint -> download -> Claude Vision -> fat_invoice / fat_invoice_item.

A coordenação de arquivos + leitura + persistência mora em
`coleta_invoices/invoice_pipeline.py`; o cliente de navegação em
`commons/sharepoint/`; o cliente do Vision em `commons/vision/`. Aqui a
orquestração da varredura por loja + o contrato de fluxo.
"""

from __future__ import annotations

import json
from datetime import date

from commons.db import conexao
from commons.exception import BusinessException
from commons.logging_config import get_logger
from commons.paths import DOWNLOAD_DIR, READ_DIR, ROOT
from domain.config import Config
from domain.service.invoice_service import (
    abrir_coleta,
    falhar_login,
    falhar_navegacao,
    fetch_all_configs,
    registrar_download,
    registrar_navegacao,
)
from domain.service import notificacao_service, sistema_service
from domain.sistemas import Sistema

log = get_logger(__name__)

# dim_loja.id -> sistema de SharePoint correspondente (para o inventario/alertas).
_SISTEMA_POR_LOJA = {
    1: Sistema.SHAREPOINT_WINDERMERE,
    2: Sistema.SHAREPOINT_DRPHILLIPS,
}

# Modo de leitura das invoices. False = Batch API (50% mais barato, resultado no
# proximo tick); True = processa na hora. Toggle em vez de flag de CLI.
MODO_SINCRONO = True

# Inicio da producao: semanas anteriores ja foram conciliadas a mao pelo
# cliente e nao podem ser baixadas (ex.: 10 PDFs soltos em SET/28 A 04 da
# Dr. Phillips). Compara com a segunda-feira da semana
# (spec-coleta-arquivos-soltos R11).
INICIO_COLETA = date(2026, 10, 5)


def invoices_flow(config: Config) -> None:
    """Baixa as invoices da semana, manda ler pelo Vision e persiste.

    Imports tardios de proposito: `playwright` e `anthropic` sao pesados e so
    fazem falta aqui.
    """
    try:
        _coletar(config)
    except BusinessException as exc:
        log.warning("invoices: caso de negocio - %s", exc)


def _coletar(config: Config) -> None:
    from coleta_invoices.invoice_pipeline import (
        process_invoices_batch,
        process_invoices_sync,
    )
    from commons.sharepoint import (
        current_month_folder,
        current_year_folder,
        nome_pasta_semana,
        process_all_configs,
        semanas_da_coleta,
    )

    username = config.sharepoint.usuario
    password = config.sharepoint.senha
    api_key = config.vision.api_key
    ai_model = config.vision.modelo
    today = date.today()
    # Semana anterior + atual em toda execucao: o cliente pode incluir nota na
    # pasta da semana anterior dias depois. Arquivo ja baixado e pulado pelo
    # nome, entao revarrer so traz o que e novo (spec-coleta-arquivos-soltos R4).
    # Cada referencia e a segunda-feira da semana (R8/R9).
    referencias = _semanas_a_partir_do_corte(semanas_da_coleta(today))
    if not referencias:
        log.info("Nenhuma semana a partir do inicio da coleta; nada a varrer.")
        return

    log.info("Data : %s", today.strftime('%d/%m/%Y'))
    for ref in referencias:
        log.info(
            "Caminho : Invoices Fornecedores / %s / _Invoices para Lancamento / %s / %s",
            current_year_folder(ref), current_month_folder(ref), nome_pasta_semana(ref),
        )

    with conexao(config.banco) as conn:
        records = fetch_all_configs(conn)
        all_output: list[dict] = []
        log.info("%s config(s) encontrado(s) no banco.", len(records))

        def on_record_done(r: dict) -> None:
            """Fecha o caso 'coleta' desta loja.

            A varredura tem vida própria: existe mesmo quando não achou
            arquivo, que é o caso mais comum de falha e o que antes sumia.
            """
            record = r["record"]
            referencia = r["referencia"]
            final_path = r["final_path"]
            error = r.get("error")
            achados = _contar_arquivos(r["entries"])
            sistema = _SISTEMA_POR_LOJA.get(record["id"])

            id_coleta = abrir_coleta(conn, record["id"], referencia)
            if r["status"]:
                registrar_navegacao(conn, id_coleta, final_path, achados)
                if achados:
                    registrar_download(conn, id_coleta, len(r.get("downloaded", [])))
                if sistema:
                    sistema_service.registrar_acesso(conn, sistema, ok=True)
            elif r.get("error_tipo") == "login":
                falhar_login(conn, id_coleta, str(error))
                notificacao_service.registrar_erro(
                    f"login SharePoint loja id={record['id']}", mensagem=str(error))
                if sistema:
                    sistema_service.registrar_acesso(conn, sistema, ok=False, mensagem=str(error))
            else:
                falhar_navegacao(conn, id_coleta, str(error))
                notificacao_service.registrar_erro(
                    f"navegacao SharePoint loja id={record['id']}", mensagem=str(error))

            label = "OK  " if r["status"] else "ERRO"
            log.info(
                "[%s] loja id=%s files=%s processo=%s -> gravado",
                label, record['id'], achados, id_coleta,
            )
            if error:
                log.info("erro: %s", error)

            all_output.append({
                "config":     record,
                "status":     r["status"],
                "final_path": final_path,
                "entries":    r["entries"],
                "downloaded": r.get("downloaded", []),
                "id_coleta":  id_coleta,
            })

        process_all_configs(
            records, username, password, lambda record: referencias,
            headless=True,
            keep_open=False,
            download_dir=DOWNLOAD_DIR,
            skip_dirs=[READ_DIR],
            on_record_done=on_record_done,
        )

        if api_key:
            if MODO_SINCRONO:
                log.info("Modo: sincrono (MODO_SINCRONO = True)")
                process_invoices_sync(conn, api_key, all_output, ai_model, DOWNLOAD_DIR, READ_DIR)
            else:
                log.info("Modo: Batch API (MODO_SINCRONO = False)")
                process_invoices_batch(conn, api_key, all_output, ai_model, DOWNLOAD_DIR, READ_DIR)
        else:
            log.warning("AVISO: 'schiavon_key_vision' nao configurado no profile - leitura de invoices ignorada.")

        output_file = ROOT / "resultado.json"
        output_file.write_text(
            json.dumps(
                [{k: v for k, v in r.items() if k != "id_coleta"} for r in all_output],
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        log.info("Resultado salvo em: %s", output_file.name)


def _semanas_a_partir_do_corte(semanas: list[date]) -> list[date]:
    """Tira as semanas anteriores a `INICIO_COLETA` (conciliadas a mao)."""
    puladas = [s for s in semanas if s < INICIO_COLETA]
    if puladas:
        log.info(
            "Semana(s) %s antes do inicio da coleta (%s): nao varridas.",
            ", ".join(s.strftime('%d/%m/%Y') for s in puladas),
            INICIO_COLETA.strftime('%d/%m/%Y'),
        )
    return [s for s in semanas if s >= INICIO_COLETA]


def _contar_arquivos(entries: list[dict]) -> int:
    """So arquivos soltos na pasta da semana contam; subpastas (LANCADAS,
    PENDENCIAS...) sao ignoradas pela coleta (spec-coleta-arquivos-soltos)."""
    return sum(1 for e in entries if e["type"] == "arquivo")


coletar_invoices = invoices_flow

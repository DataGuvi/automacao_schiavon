"""Pipeline de leitura e persistência de invoices via Claude Vision."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from commons.logging_config import get_logger
from domain.service.invoice_service import config_from_filename, save_invoice
from commons.vision import (
    BatchItem,
    collect_batch_results,
    read_invoice,
    submit_batch,
    wait_for_batch,
)

log = get_logger(__name__)

# Teto da espera pelo lote: o tick nao pode ficar preso o dia todo (spec-vision-batch R3).
ESPERA_MAXIMA_LOTE_S = 3600


def collect_items(conn, results: list[dict], download_dir: Path) -> list[BatchItem]:
    """
    Monta a lista de BatchItem combinando:
      1. Arquivos baixados na execução atual (de results)
      2. Arquivos órfãos já presentes em download_dir de execuções anteriores
    """
    items: list[BatchItem] = []
    current_files: set[str] = set()

    for r in results:
        if not r["downloaded"]:
            continue
        record = r["config"]
        for file_str in r["downloaded"]:
            fp = Path(file_str)
            current_files.add(fp.name)
            items.append(BatchItem(
                file_path=fp,
                config_id=record["id"],
                config_name=record["name"],
            ))

    extensions = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif"}
    for fp in download_dir.iterdir():
        if fp.suffix.lower() not in extensions or fp.name in current_files:
            continue
        cfg = config_from_filename(fp.name)
        if cfg is None:
            log.warning("Arquivo orfao sem config reconhecido: %s (ignorado)", fp.name)
            continue
        config_id, config_name = cfg
        # Sem dependencia de execution_log: o proprio arquivo vira um caso.
        log.info("+ Orfao incluido: %s", fp.name)
        items.append(BatchItem(
            file_path=fp,
            config_id=config_id,
            config_name=config_name,
        ))

    return items


def _persist_and_move(conn, item: BatchItem, invoice_data, read_dir: Path) -> float:
    """Salva invoice no banco, move o arquivo para read_dir. Retorna custo.

    Arquivos são agrupados por dia de leitura, em `invoice_DD-MM-AAAA/`, para
    não acumular tudo solto numa única pasta.
    """
    header_id, n_items = save_invoice(
        conn,
        id_loja=item.config_id,
        file_path=item.file_path,
        data=invoice_data,
        custo=invoice_data.cost_read,
    )
    log.info("header_id=%s itens=%s custo=$%.4f", header_id, n_items, invoice_data.cost_read)
    day_dir = read_dir / f"invoice_{date.today().strftime('%d-%m-%Y')}"
    day_dir.mkdir(parents=True, exist_ok=True)
    dest = day_dir / item.file_path.name
    item.file_path.rename(dest)
    log.info("-> files/read_files/%s/%s", day_dir.name, item.file_path.name)
    return invoice_data.cost_read


def process_invoices_batch(
    conn,
    api_key: str,
    results: list[dict],
    model: str,
    download_dir: Path,
    read_dir: Path,
) -> None:
    """
    Envia todos os arquivos para o Batch API de uma vez (50% mais barato).
    Inclui arquivos da execução atual e órfãos de execuções anteriores.
    """
    items = collect_items(conn, results, download_dir)
    if not items:
        log.info("Nenhum arquivo para processar.")
        return

    log.info("Batch API - %s arquivo(s) modelo=%s", len(items), model)

    batch_id, id_map = submit_batch(items, api_key, model)
    wait_for_batch(
        batch_id, api_key, poll_interval=20, max_wait_seconds=ESPERA_MAXIMA_LOTE_S,
    )

    log.info("Processando resultados...")
    batch_results = collect_batch_results(batch_id, api_key, id_map, model)

    total_cost = 0.0
    read_dir.mkdir(parents=True, exist_ok=True)

    for item, invoice_data, error in batch_results:
        log.info("[%s] %s", item.config_name, item.file_path.name)
        if error or invoice_data is None:
            log.error("Erro: %s", error)
            continue
        log.info(
            "confianca : %.0f%% status=%s",
            invoice_data.reading_confidence, invoice_data.reading_status,
        )
        if invoice_data.reading_notes:
            log.info("notas IA : %s", invoice_data.reading_notes[:120])
        try:
            total_cost += _persist_and_move(conn, item, invoice_data, read_dir)
        except Exception as exc:
            conn.rollback()
            log.error("Erro ao gravar no banco: %s", exc)

    log.info("Custo total do batch: $%.4f USD", total_cost)


def process_invoices_sync(
    conn,
    api_key: str,
    results: list[dict],
    model: str,
    download_dir: Path,
    read_dir: Path,
) -> None:
    """
    Processa cada arquivo imediatamente via chamada síncrona à API.
    Mais rápido para poucos arquivos; sem desconto de preço.
    """
    items = collect_items(conn, results, download_dir)
    if not items:
        log.info("Nenhum arquivo para processar.")
        return

    log.info("Sincrono - %s arquivo(s) modelo=%s", len(items), model)

    total_cost = 0.0
    read_dir.mkdir(parents=True, exist_ok=True)

    for item in items:
        total_cost += _ler_e_gravar_uma(conn, item, api_key, model, read_dir)

    log.info("invoices: custo total da leitura sincrona USD %.4f", total_cost)


def _ler_e_gravar_uma(conn, item, api_key: str, model: str, read_dir: Path) -> float:
    """Lê UMA nota e grava. Devolve o custo; 0.0 se falhou.

    Laço de item: `except Exception` aqui é a rede de segurança que a
    governança autoriza — uma nota ilegível não pode parar as outras. Ficava
    como dois `try` dentro de `process_invoices_sync` (um da leitura, um da
    gravação); separado, cada função tem um `try` e o laço fica legível.
    """
    log.info("invoices: [%s] %s", item.config_name, item.file_path.name)
    try:
        invoice_data = read_invoice(item.file_path, api_key, model)
    except Exception as exc:  # noqa: BLE001 — ver docstring
        log.error("invoices: falha na leitura de %s - %s", item.file_path.name, exc)
        return 0.0

    log.info("invoices: %s - confianca %.0f%% status=%s", item.file_path.name,
             invoice_data.reading_confidence, invoice_data.reading_status)
    if invoice_data.reading_notes:
        log.info("invoices: %s - notas da IA: %s", item.file_path.name,
                 invoice_data.reading_notes[:120])

    return _gravar_uma(conn, item, invoice_data, read_dir)


def _gravar_uma(conn, item, invoice_data, read_dir: Path) -> float:
    """Persiste a nota lida. Devolve o custo; 0.0 e rollback se falhou."""
    try:
        return _persist_and_move(conn, item, invoice_data, read_dir)
    except Exception as exc:  # noqa: BLE001 — laco de item
        conn.rollback()
        log.error("invoices: falha ao gravar %s no banco - %s",
                  item.file_path.name, exc)
        return 0.0

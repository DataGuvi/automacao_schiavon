"""Lê a planilha Conferência Manual (Google Sheets -> banco).

Preâmbulo do pipeline, junto do FLUXO 1 (Sinônimos): lê a coluna `Conferido`
da aba `Conferência Manual` e grava `fat_invoice.conferido_manual_em` para
quem foi marcado. É isso que tira a invoice da tela "Conferências manuais" e
leva para "Invoices sem divergência" no Grafana — ver
`spec-conferencia-manual-planilha.md`.

A leitura/dedupe vivem em `conciliacao/conferencia_manual.py` (testável sem
Postgres). Aqui só a orquestração + o contrato de fluxo.

**Preâmbulo não derruba o pipeline.** `CONFERENCIA_MANUAL_SHEET_ID` ausente,
Sheets fora do ar, banco fora do ar ou erro de leitura -> aviso e o pipeline
segue; a confirmação é lida de novo na próxima execução.
"""

from __future__ import annotations

from commons.exception import BusinessException, ConfigException, DataAccessException
from commons.logging_config import get_logger
from conciliacao.conferencia_manual import invoices_conferidas
from domain.config import Config
from domain.service.conciliacao_service import marcar_conferido_manual

log = get_logger(__name__)


def conferencia_manual_flow(config: Config) -> None:
    """Abre conexão, lê o checkbox `Conferido` da planilha, marca no banco, sai."""
    from commons.db import conexao

    sheet_id = config.conferencia_manual_sheet_id or None
    if not sheet_id:
        log.info("conferencia_manual: CONFERENCIA_MANUAL_SHEET_ID ausente - nada a sincronizar")
        return

    # Um `try` so, varios handlers: o preambulo nao derruba o pipeline em
    # NENHUM dos casos (sem banco, regra de negocio, ou falha inesperada),
    # so muda a mensagem (mesmo contrato de sinonimos_flow).
    try:
        conferidas = invoices_conferidas(sheet_id)
        if not conferidas:
            log.info("conferencia_manual: nenhuma linha marcada Conferido na planilha")
            return
        with conexao(config.banco) as conn:
            n = marcar_conferido_manual(conn, conferidas)
        log.info("conferencia_manual: %s invoice(s) marcada(s) como conferida(s) manualmente", n)
    except BusinessException as exc:
        log.warning("conferencia_manual: %s", exc)
    except (ConfigException, DataAccessException) as exc:
        log.warning("conferencia_manual: sem conexao com o banco, pulando (%s)", exc)
    except Exception as exc:  # noqa: BLE001 — preambulo nao derruba o pipeline
        log.warning("conferencia_manual: sincronizacao falhou, pulando nesta execucao (%s)", exc)

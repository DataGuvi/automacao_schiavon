"""spec-vision-batch: o fluxo 2 le as invoices em lote."""

from pathlib import Path
from unittest.mock import patch

from coleta_invoices import invoice_pipeline
from commons.vision import BatchItem
from crawler.flow import invoices_flow


def test_modo_padrao_e_lote():
    assert invoices_flow.MODO_SINCRONO is False


def test_lote_limita_a_espera():
    item = BatchItem(Path("a.pdf"), 1, "loja")
    with patch.object(invoice_pipeline, "collect_items", return_value=[item]), \
         patch.object(invoice_pipeline, "submit_batch", return_value=("b1", {})), \
         patch.object(invoice_pipeline, "wait_for_batch") as espera, \
         patch.object(invoice_pipeline, "collect_batch_results", return_value=[]):
        invoice_pipeline.process_invoices_batch(
            None, "k", [], "m", Path("d"), Path("r"),
        )
    assert espera.call_args.kwargs["max_wait_seconds"] == invoice_pipeline.ESPERA_MAXIMA_LOTE_S

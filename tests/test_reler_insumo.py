"""spec-anotacao-insumo R9: anotacao duvidosa e relida pelo modelo forte antes de travar."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coleta_invoices import invoice_pipeline as pipe
from commons.vision import BatchItem
from commons.vision.schema import InvoiceData

ITEM = BatchItem(Path("a.pdf"), 1, "loja")


def _nota(geral, custo=0.1):
    return InvoiceData(general_handwritten_notes=geral, cost_read=custo, items=[], reading_confidence=90)


def _reler(inicial, forte, modelo="forte"):
    with patch.object(pipe, "read_invoice", return_value=forte) as lido:
        return pipe._reler_se_incerto(ITEM, inicial, "k", modelo), lido


def test_incerto_vira_insumo_com_a_leitura_do_modelo_forte():
    nota, lido = _reler(_nota("INSUMO_INCERTO: FINAL WRAP"), _nota("Insumo Burger", 0.5))
    assert nota.general_handwritten_notes == "Insumo Burger"
    assert nota.cost_read == 0.6 and lido.call_args.args[2] == "forte"


def test_incerto_continua_incerto_se_o_modelo_forte_tambem_duvida():
    nota, _ = _reler(_nota("INSUMO_INCERTO: x"), _nota("INSUMO_INCERTO: FINAL WRAP"))
    assert nota.general_handwritten_notes == "INSUMO_INCERTO: FINAL WRAP"


def test_nao_incerto_nao_chama_o_modelo_forte():
    for texto in ("Insumo", None):
        nota, lido = _reler(_nota(texto), _nota("x"))
        assert nota.general_handwritten_notes == texto and lido.call_count == 0


def test_sem_modelo_forte_nao_rele():
    nota, lido = _reler(_nota("DONE"), _nota("x"), modelo="")
    assert lido.call_count == 0 and nota.general_handwritten_notes == "DONE"


def test_falha_na_releitura_mantem_a_original_e_registra_o_erro():
    with patch.object(pipe, "read_invoice", side_effect=ValueError("boom")),             patch.object(pipe.notificacao_service, "registrar_erro") as registrar:
        nota = pipe._reler_se_incerto(ITEM, _nota("INSUMO_INCERTO: x"), "k", "forte")
    assert nota.general_handwritten_notes == "INSUMO_INCERTO: x"
    assert registrar.call_count == 1


def test_modelo_forte_igual_ao_base_nao_rele():
    with patch.object(pipe, "read_invoice") as lido:
        nota = pipe._reler_se_incerto(ITEM, _nota("INSUMO_INCERTO: x"), "k", "mesmo", "mesmo")
    assert lido.call_count == 0 and nota.general_handwritten_notes == "INSUMO_INCERTO: x"


def test_modelo_forte_sem_anotacao_mantem_a_duvida_original_e_soma_o_custo():
    nota, _ = _reler(_nota("INSUMO_INCERTO: FINAL WRAP"), _nota(None, 0.5))
    assert nota.general_handwritten_notes == "INSUMO_INCERTO: FINAL WRAP"
    assert nota.cost_read == 0.6


def _lote(forte_por_arquivo=None, erro=None):
    """Patches do segundo lote: devolve `(item, nota_forte, None)` para cada arquivo enviado."""
    def coletar(batch_id, api_key, id_map, modelo):
        return [(it, (forte_por_arquivo or {}).get(it.file_path), erro) for it in enviados]
    enviados = []
    def enviar(itens, api_key, modelo):
        enviados.extend(itens)
        return "b2", {}
    return enviar, coletar, enviados


def test_lote_rele_so_as_incertas_em_um_segundo_lote():
    outro = BatchItem(Path("b.pdf"), 2, "loja")
    lidas = [(ITEM, _nota("INSUMO_INCERTO: FINAL WRAP")), (outro, _nota("Insumo"))]
    enviar, coletar, enviados = _lote({ITEM.file_path: _nota("Insumo Burger", 0.5)})
    with patch.object(pipe, "submit_batch", side_effect=enviar), \
            patch.object(pipe, "wait_for_batch"), \
            patch.object(pipe, "collect_batch_results", side_effect=coletar):
        saida = pipe._reler_incertas_em_lote(lidas, "k", "forte", "base")
    assert enviados == [ITEM]
    assert saida[0][1].general_handwritten_notes == "Insumo Burger" and saida[0][1].cost_read == 0.6
    assert saida[1][1].general_handwritten_notes == "Insumo"


def test_lote_sem_incertas_nao_envia_nada():
    with patch.object(pipe, "submit_batch") as enviar:
        saida = pipe._reler_incertas_em_lote([(ITEM, _nota("Insumo"))], "k", "forte", "base")
    assert enviar.call_count == 0 and len(saida) == 1


def test_falha_do_lote_de_releitura_mantem_as_originais_e_registra():
    lidas = [(ITEM, _nota("INSUMO_INCERTO: FINAL WRAP"))]
    with patch.object(pipe, "submit_batch", side_effect=TimeoutError("lote")), \
            patch.object(pipe.notificacao_service, "registrar_erro") as registrar:
        saida = pipe._reler_incertas_em_lote(lidas, "k", "forte", "base")
    assert saida == lidas and registrar.call_count == 1

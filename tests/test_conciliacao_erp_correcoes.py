"""Testes das correcoes do code-review do FLUXO 4 (.claude/rules/spec-correcao-reconcile-erp.md).

Sem banco e sem navegador: fakes em memoria.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from commons.exception import DataAccessException  # noqa: E402
from crawler.flow import reconcile_erp_flow as flow  # noqa: E402
from domain.conciliacao_codes import IssueCode  # noqa: E402
from domain.enums import StatusConciliacao, StatusExecucao  # noqa: E402
from domain.service import conciliacao_service as svc  # noqa: E402
from tests.test_reconcile_erp import _item, _po  # noqa: E402
from conciliacao.reconcile_erp import reconcile_items_against_po  # noqa: E402


def _h(id_, loja, cod_status=None):
    return {"id": id_, "id_loja": loja, "cod_status": cod_status}


# R2
def test_insumo_nao_e_conferido():
    assert svc._status_conciliacao([IssueCode.SKIPPED_INSUMO_ANNOTATION]) == StatusConciliacao.NAO_COMPARADO


# R3
def test_nota_nao_tentada_da_semana_anterior_nao_prende_a_semana_atual():
    headers = flow._selecionar_headers([_h(1, 10, int(StatusExecucao.PENDENTE))], [_h(2, 10), _h(3, 20)], [])
    assert [h["id"] for h in headers] == [1, 2, 3]


def test_nota_em_erro_entra_junto_com_a_semana_atual():
    headers = flow._selecionar_headers([_h(1, 10, int(StatusExecucao.ERRO_NAVEGACAO))], [_h(2, 10)], [])
    assert [h["id"] for h in headers] == [1, 2]


# R4
def test_reprocesso_entra_sem_duplicar():
    headers = flow._selecionar_headers([], [_h(2, 10)], [_h(2, 10), _h(9, 10)])
    assert [h["id"] for h in headers] == [2, 9]


# R5
class _CursorQuebrado:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        raise psycopg2.Error("boom")


class _ConnQuebrada:
    rollbacks = 0

    def cursor(self, **k):
        return _CursorQuebrado()

    def rollback(self):
        self.rollbacks += 1


@pytest.mark.parametrize("fn", [
    svc.fetch_supplier_aliases, svc.fetch_fornecedores, svc.fetch_item_sinonimos,
    svc.fetch_invoice_headers_reprocesso,
    lambda c: svc.fetch_invoice_headers_for_reconciliation(c, None, None),
    lambda c: svc.fetch_invoice_items_by_headers(c, [1]),
])
def test_leitura_com_erro_do_driver_vira_data_access(fn):
    conn = _ConnQuebrada()
    with pytest.raises(DataAccessException):
        fn(conn)
    assert conn.rollbacks == 1


# R6
def test_qty_invoice_coerente_em_grupo_com_cases_misto():
    po = [_po(1, "Chicken Breast", supplier_unit_id="X1", ordered="5", received="5",
              invoiced_total_cost="100", unit="Case")]
    itens = [
        _item(1, "Chicken Breast", "20", "2.5", "50", item_code="X1", cases="2"),
        _item(2, "Chicken Breast", "20", "2.5", "50", item_code="X1"),
    ]
    linhas = reconcile_items_against_po(itens, po)["items"]
    assert [l["qty_invoice"] for l in linhas] == [Decimal("20"), Decimal("20")]


# R1
@pytest.mark.parametrize("numero,data,chamou", [
    (None, "2026-01-05", False),
    ("  ", "2026-01-05", False),
    ("123", None, False),
    ("123", "2026-01-05", True),
])
def test_fill_receiving_so_com_numero_e_data(monkeypatch, numero, data, chamou):
    chamadas = []
    po = _po(1, "Chicken Breast", supplier_unit_id="X1", ordered="5")
    monkeypatch.setattr(flow, "open_worksheets", lambda *a: None)
    monkeypatch.setattr(flow, "search_purchase_orders_by_supplier", lambda *a, **k: [{"href": "h"}])
    monkeypatch.setattr(flow, "open_purchase_order", lambda *a: None)
    monkeypatch.setattr(flow, "scrape_po_items", lambda *a: [])
    monkeypatch.setattr(flow, "to_po_lines", lambda *a: [po])
    monkeypatch.setattr(flow, "escolher_po_por_itens", lambda *a: 0)
    monkeypatch.setattr(flow, "fill_receiving_invoice_info", lambda *a: chamadas.append(a))
    header = {"id": 1, "invoice_number": numero, "invoice_date": data, "supplier_name": "Acme"}
    assert flow._buscar_po(None, "u", header, [], {}, lambda nome: None) == [po]
    assert bool(chamadas) is chamou
    assert all(c[1] != "None" for c in chamadas)

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
    monkeypatch.setattr(flow, "search_purchase_orders_by_invoice", lambda *a, **k: [])
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


# spec-pack-size-coluna R6: Leblon, PO Single Unit conta unidades do pack
def test_leblon_pack_multiplicado_confere_com_po_single_unit():
    po = [
        _po(1, "Queijo Fresc Minas", supplier_unit_id="1", ordered="12", received="12",
            invoiced_total_cost="53.90", unit="Single Unit"),
        _po(2, "Queijo Coalho 2LB", supplier_unit_id="381", ordered="6", received="6",
            invoiced_total_cost="65.70", unit="Single Unit"),
    ]
    itens = [
        _item(1, "MIMOSO MINAS FRESCAL CHEESE 12oz", "12", "53.90", "53.90", item_code="1", cases="1"),
        _item(2, "DA ROCA QUEIJO COALHO 2LBS", "6", "65.70", "65.70", item_code="381", cases="1"),
    ]
    resultado = reconcile_items_against_po(itens, po)
    assert not resultado["has_issue"]
    assert all(IssueCode.QTY_MISMATCH_PO not in l["issue_codes"] for l in resultado["items"])



# spec-po-zerado-ultimo-recurso: Gominas 5114, linha 2914 zerada no PO
def _po_gominas():
    return [
        _po(0, "Bala de Gelatina Amoras", supplier_unit_id="2314", ordered="12", received="12",
            invoiced_total_cost="24.90", unit="Single Unit"),
        _po(1, "Bala de Gelatina Ovos Fritos", supplier_unit_id="2914", ordered="24",
            received="0", invoiced_total_cost="0", unit="Single Unit"),
        _po(2, "Fini Fried Eggs", supplier_unit_id="fini4", ordered="0", received="24",
            invoiced_total_cost="43.98", unit="Single Unit"),
    ]


def _itens_gominas():
    return [
        _item(1, "Fini G Amora 12 x 80g", "12", "24.90", "24.90", item_code="2314", cases="1"),
        _item(2, "Fini G Ovo Frito 12x80g", "24", "17.00", "34.00", item_code="2914", cases="2"),
    ]


def test_linha_zerada_casa_por_codigo_como_ultimo_recurso():
    resultado = reconcile_items_against_po(_itens_gominas(), _po_gominas())
    amora, ovo = resultado["items"]
    assert amora["issue_codes"] == []
    assert ovo["match_level"] == "codigo" and ovo["item_name_po"] == "Bala de Gelatina Ovos Fritos"
    assert IssueCode.PRICE_MISMATCH_PO in ovo["issue_codes"]
    assert IssueCode.QTY_MISMATCH_PO in ovo["issue_codes"]
    assert IssueCode.NO_PO_FOR_ITEM not in ovo["issue_codes"]
    assert ovo["price_diff"] == Decimal("34.00")


def test_linha_zerada_ja_reclamada_nao_casa_de_novo():
    itens = [
        _item(1, "Fini G Ovo Frito 12x80g", "24", "17.00", "34.00", item_code="2914"),
        _item(2, "Outro item", "1", "5.00", "5.00", item_code="2914"),
    ]
    po = [_po(1, "Bala de Gelatina Ovos Fritos", supplier_unit_id="2914", ordered="24",
              received="0", invoiced_total_cost="0", unit="Single Unit")]
    resultado = reconcile_items_against_po(itens, po)
    assert [l["match_level"] for l in resultado["items"]].count("unmatched") == 1


def test_linha_ativa_por_nome_ganha_da_zerada():
    po = [
        _po(0, "Agua Pure Life", supplier_unit_id="111", ordered="160", received="0",
            invoiced_total_cost="0", unit="Single Unit"),
        _po(1, "Pack Nestle Pure Life Purified Water 40 0.5L", supplier_unit_id="222", ordered="4",
            received="4", invoiced_total_cost="21.80", unit="Single Unit"),
    ]
    itens = [_item(1, "Nestle Pure Life - Purified Water - 40/0.5L", "4", "5.45", "21.80",
                   item_code="111")]
    linha = reconcile_items_against_po(itens, po)["items"][0]
    assert linha["item_name_po"] == "Pack Nestle Pure Life Purified Water 40 0.5L"


# spec-categoria-insumo-carne
def _processar(monkeypatch, categoria=None, insumo_na_nota=False, aliases=None, conn=object(), alterou=True):
    chamadas = {"busca": 0, "skip": 0, "classificou": []}
    monkeypatch.setattr(flow, "_buscar_po", lambda *a, **k: chamadas.__setitem__("busca", chamadas["busca"] + 1) or [])
    monkeypatch.setattr(flow, "_gravar_skip_insumo", lambda *a: chamadas.__setitem__("skip", chamadas["skip"] + 1))
    monkeypatch.setattr(flow, "_gravar_resultado", lambda *a, **k: None)
    monkeypatch.setattr(
        flow, "classificar_fornecedor",
        lambda c, i, cat, somente_sem_categoria=False: chamadas["classificou"].append((i, str(cat), somente_sem_categoria)) or alterou,
    )
    forn = {"id": 7, "nome": "IDO Imports", "categoria": categoria}
    header = {"id": 1, "id_processo": 9, "invoice_number": "N1", "supplier_name": "IDO Imports",
              "general_handwritten_notes": "Insumo" if insumo_na_nota else None}
    items = [_item(1, "Item", "1", "5.00", "5.00", item_code="X1")]
    totais = {"aguardando_po": 0, "sem_po": 0}
    flow._processar_invoice(None, "u", conn, header, items, {}, lambda n: forn, aliases or {}, None, totais)
    return chamadas, forn


def test_fornecedor_insumo_resolvido_por_aproximacao_pula_sem_buscar_po(monkeypatch):
    chamadas, _ = _processar(monkeypatch, categoria="insumo")
    assert chamadas["skip"] == 1 and chamadas["busca"] == 0 and chamadas["classificou"] == []


def test_insumo_lido_pela_vision_classifica_fornecedor_sem_categoria(monkeypatch):
    chamadas, forn = _processar(monkeypatch, categoria=None, insumo_na_nota=True)
    assert chamadas["skip"] == 1 and chamadas["busca"] == 0
    assert chamadas["classificou"] == [(7, "insumo", True)]
    assert forn["categoria"] == "insumo"


def test_insumo_lido_pela_vision_com_nome_pouco_parecido_nao_classifica(monkeypatch):
    # fornecedor resolvido pelo piso 90, mas o nome da nota nao chega a 95 e nao ha alias
    monkeypatch.setattr(flow, "confirmar_fornecedor_para_aprender", lambda *a: False)
    chamadas, _ = _processar(monkeypatch, categoria=None, insumo_na_nota=True)
    assert chamadas["skip"] == 1 and chamadas["classificou"] == []


def test_insumo_lido_pela_vision_com_alias_exato_classifica(monkeypatch):
    monkeypatch.setattr(flow, "confirmar_fornecedor_para_aprender", lambda *a: False)
    aliases = {flow.norm_supplier("IDO Imports"): {"canonical_id": 7, "categoria": None}}
    chamadas, _ = _processar(monkeypatch, categoria=None, insumo_na_nota=True, aliases=aliases)
    assert chamadas["classificou"] == [(7, "insumo", True)]


def test_aprendizado_de_insumo_nao_sobrescreve_e_nao_derruba(monkeypatch):
    chamadas, forn = _processar(monkeypatch, categoria="mercearia", insumo_na_nota=True, alterou=False)
    assert chamadas["skip"] == 1                      # a nota continua pulada
    assert forn["categoria"] == "mercearia"           # categoria existente intocada (a trava e no SQL)

    def _quebra(*a, **k):
        raise DataAccessException("boom")
    monkeypatch.setattr(flow, "classificar_fornecedor", _quebra)
    flow._aprender_insumo(object(), {"id": 7, "nome": "IDO Imports"}, "IDO Imports", {flow.norm_supplier("IDO Imports"): {}})


def test_categoria_do_fornecedor_alias_ou_resolvido():
    aliases = {flow.norm_supplier("Black Bull"): {"categoria": "carne"}}
    assert flow._categoria_do_fornecedor("Black Bull", aliases, None) == "carne"
    assert flow._categoria_do_fornecedor("Black Bull Foods", {}, {"categoria": "carne"}) == "carne"
    assert flow._categoria_do_fornecedor("Zzz", {}, None) is None


def test_carne_resolvida_por_aproximacao_recebe_tolerancia_zero():
    from conciliacao.reconcile_erp import _resolver_tolerancia_preco
    assert _resolver_tolerancia_preco("Black Bull Foods", flow._categoria_do_fornecedor(
        "Black Bull Foods", {}, {"categoria": "carne"})) == (Decimal("0"), Decimal("0"))


def test_classificar_fornecedor_so_sem_categoria_filtra_no_sql():
    class Cur:
        rowcount = 1
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params): self.sql, self.params = sql, params

    class Conn:
        def __init__(self): self.cur = Cur(); self.commits = 0
        def cursor(self): return self.cur
        def commit(self): self.commits += 1

    conn = Conn()
    assert svc.classificar_fornecedor(conn, 7, "insumo", somente_sem_categoria=True) is True
    assert "categoria IS NULL OR categoria = 'outros'" in conn.cur.sql
    assert conn.cur.params == ("insumo", 7) and conn.commits == 1
    svc.classificar_fornecedor(conn, 7, "carne")
    assert "IS NULL" not in conn.cur.sql


def test_classificar_fornecedor_erro_do_driver_vira_data_access():
    conn = _ConnQuebrada()
    with pytest.raises(DataAccessException):
        svc.classificar_fornecedor(conn, 7, "carne")
    assert conn.rollbacks == 1


def test_insumo_por_aproximacao_sem_nome_confirmado_nao_pula(monkeypatch):
    # spec-categoria-insumo-carne R7: 'Prime Meats' resolvido para um 'Prime' insumo nao e pulado
    monkeypatch.setattr(flow, "confirmar_fornecedor_para_aprender", lambda *a: False)
    chamadas, _ = _processar(monkeypatch, categoria="insumo")
    assert chamadas["skip"] == 0 and chamadas["busca"] == 1

"""Spec spec-ordenar-po-por-data-criacao: busca por fornecedor ordenada por
'Date Created' (mais recente primeiro) e corte nos 20 mais recentes quando
vem candidato demais pra desempatar abrindo um por um."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.flow import reconcile_erp_flow as flow  # noqa: E402
from tests.test_reconcile_erp import _item, _po  # noqa: E402

HEADER = {"id": 1, "invoice_number": "LU1", "invoice_date": "2026-01-05", "supplier_name": "Leblon foods"}


def _preparar(monkeypatch, n_achados: int, indice_escolhido: int):
    """25 achados de nome (hrefs 'h0'..'h24'), cada um com um POLine distinto
    pra provar que o `indice` devolvido por `escolher_po_por_itens` aponta
    pra posicao certa mesmo depois do corte (R6)."""
    achados_nome = [{"href": f"h{i}"} for i in range(n_achados)]
    pos = {i: _po(i, f"Item {i}", supplier_unit_id=f"X{i}") for i in range(n_achados)}
    abertos: list[str] = []

    monkeypatch.setattr(flow, "open_worksheets", lambda *a: None)
    monkeypatch.setattr(flow, "search_purchase_orders_by_invoice", lambda *a, **k: [])
    monkeypatch.setattr(flow, "search_purchase_orders_by_supplier", lambda *a, **k: achados_nome)
    monkeypatch.setattr(flow, "open_purchase_order", lambda page, href: abertos.append(href))
    monkeypatch.setattr(flow, "scrape_po_items", lambda *a: [])
    # Indice do href aberto (ex. 'h7' -> 7) decide qual POLine volta, pra
    # comprovar que o item certo (nao so a posicao) chega ao fim.
    monkeypatch.setattr(flow, "to_po_lines", lambda *a: [pos[len(abertos) - 1]])
    monkeypatch.setattr(flow, "escolher_po_por_itens", lambda *a: indice_escolhido)
    monkeypatch.setattr(flow, "fill_receiving_invoice_info", lambda *a: None)
    monkeypatch.setattr(flow, "_aprender_nome_catapult", lambda *a, **k: None)
    return achados_nome, pos, abertos


def test_mais_de_20_candidatos_corta_nos_20_primeiros_mais_recentes(monkeypatch):
    """`search_purchase_orders_by_supplier` ja devolve ordenado por 'Date
    Created' descendente (R1/R2) - os 20 PRIMEIROS da lista de 25 sao os
    mais recentes. Com candidato demais, abre e compara so esses 20, nunca
    os 5 mais antigos (R5)."""
    achados_nome, pos, abertos = _preparar(monkeypatch, n_achados=25, indice_escolhido=7)
    items = [_item(1, "Item 7", "1", "10.00", "10.00")]

    resultado = flow._buscar_po(None, "u", HEADER, items, {}, lambda n: None)

    # 20 abertos pra desempatar (so os mais recentes, nunca h20..h24) + 1
    # reabertura do escolhido (`_abrir_po_escolhido`, pra gravar Receiving
    # Information nele).
    assert abertos == [f"h{i}" for i in range(20)] + ["h7"]
    assert resultado == [pos[7]]  # indice 7 continua apontando pro item certo


def test_20_candidatos_ou_menos_abre_todos_sem_cortar(monkeypatch):
    achados_nome, pos, abertos = _preparar(monkeypatch, n_achados=20, indice_escolhido=19)
    items = [_item(1, "Item 19", "1", "10.00", "10.00")]

    resultado = flow._buscar_po(None, "u", HEADER, items, {}, lambda n: None)

    assert abertos == [f"h{i}" for i in range(20)] + ["h19"]
    assert resultado == [pos[19]]


def test_invoice_reference_com_mais_de_20_nao_corta_desiste_igual_a_hoje(monkeypatch):
    """Busca por Invoice Reference nao e ordenada por data (R4) - candidato
    demais continua desistindo (`cortar_mais_recentes=False` por padrao),
    comportamento inalterado."""
    achados_invoice = [{"href": f"h{i}"} for i in range(25)]
    abertos: list[str] = []
    monkeypatch.setattr(flow, "open_worksheets", lambda *a: None)
    monkeypatch.setattr(flow, "search_purchase_orders_by_invoice", lambda *a, **k: achados_invoice)
    monkeypatch.setattr(flow, "search_purchase_orders_by_supplier", lambda *a, **k: [])
    monkeypatch.setattr(flow, "open_purchase_order", lambda page, href: abertos.append(href))
    monkeypatch.setattr(flow, "scrape_po_items", lambda *a: [])
    monkeypatch.setattr(flow, "to_po_lines", lambda *a: [])
    monkeypatch.setattr(flow, "escolher_po_por_itens", lambda *a: 0)
    monkeypatch.setattr(flow, "fill_receiving_invoice_info", lambda *a: None)
    monkeypatch.setattr(flow, "_aprender_nome_catapult", lambda *a, **k: None)
    items = [_item(1, "Item 0", "1", "10.00", "10.00")]

    resultado = flow._buscar_po(None, "u", HEADER, items, {}, lambda n: None)

    assert abertos == []  # desistiu antes de abrir qualquer candidato
    assert resultado is None

"""Testes da conciliacao Invoice x PO (conciliacao/reconcile_erp.py).

Fixtures em memoria — nao tocam o Postgres.

    python -m pytest tests/test_reconcile_erp.py -v
    python tests/test_reconcile_erp.py          (sem pytest instalado)
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from commons.matcher import POLine  # noqa: E402
from domain.service.insumo_service import (  # noqa: E402
    INSUMO_INCERTO,
    INSUMO_NAO,
    INSUMO_SIM,
    classificar_anotacao_insumo,
    separar_itens_insumo,
)
from conciliacao.reconcile_erp import (  # noqa: E402
    NO_PO_FOR_ITEM,
    PO_NAO_ENCONTRADA,
    PRICE_MISMATCH_PO,
    QTY_MISMATCH_PO,
    escolher_po_por_itens,
    reconcile_items_against_po,
)


def _po(key, item_name, supplier_unit_id=None, ordered=None, received=None,
        invoiced_total_cost=None, receipt_alias=None, unit=None):
    return POLine(
        key, supplier_unit_id, None, item_name,
        None if ordered is None else Decimal(str(ordered)),
        None if received is None else Decimal(str(received)),
        None if invoiced_total_cost is None else Decimal(str(invoiced_total_cost)),
        receipt_alias,
        unit,
    )


def _item(id_, desc, qty, unit_price, total_price=None, item_code=None,
          upc=None, handwritten=False, cases=None, handwritten_code=None):
    return {
        "id": id_, "description": desc, "quantity": Decimal(str(qty)),
        "unit_price": Decimal(str(unit_price)),
        "total_price": None if total_price is None else Decimal(str(total_price)),
        "item_code": item_code, "upc": upc,
        "handwritten_notes": handwritten,
        "cases": None if cases is None else Decimal(str(cases)),
        "handwritten_code": handwritten_code,
    }


def test_item_conferido_qty_e_preco_batem():
    po_lines = [_po(1, "Beef Brisket Boneless", supplier_unit_id="TEU45",
                     ordered="10", received="10", invoiced_total_cost="60.10")]
    items = [_item(100, "BEEF - Brisket / Maca de Peito", "10", "6.01", "60.10",
                    item_code="TEU45")]

    resultado = reconcile_items_against_po(items, po_lines)
    (linha,) = resultado["items"]
    assert linha["match_level"] == "codigo"
    assert linha["issue_codes"] == []
    assert linha["has_issue"] is False
    assert resultado["has_issue"] is False
    assert resultado["po_orphans"] == []


def test_quantidade_divergente_gera_qty_mismatch():
    po_lines = [_po(1, "Beef Brisket Boneless", supplier_unit_id="TEU45",
                     ordered="10", received="10", invoiced_total_cost="60.10")]
    # Faturou 8, mas o PO pediu/recebeu 10 -> diverge em quantidade.
    items = [_item(100, "BEEF - Brisket / Maca de Peito", "8", "6.01", "48.08",
                    item_code="TEU45")]

    resultado = reconcile_items_against_po(items, po_lines)
    (linha,) = resultado["items"]
    assert QTY_MISMATCH_PO in linha["issue_codes"]
    assert linha["has_issue"] is True


def test_item_peso_variavel_com_cases_confere_mesmo_com_ordered_diferente_de_received():
    """Caso real: Cheney Brothers, invoice 05-9100063327 (achado do cliente,
    Leandro Rocha — acougue). O Catapult guarda Ordered=3 CAIXAS e
    Received=209 LB (peso pesado na doca) na mesma linha do PO — unidades
    diferentes por natureza, nao um erro de recebimento. Antes desta regra,
    o motor exigia invoice_qty (peso) ~= Ordered (caixa) ~= Received (peso),
    e a linha saia sempre com QTY_MISMATCH_PO mesmo com tudo certo. Com
    `cases` preenchido pela leitura, a checagem passa a ser caixa x Ordered
    e Received sai da equacao.

    `item_code` aqui e o codigo IMPRESSO na nota (catalogo da Cheney,
    '10112883') — confirmado contra o Catapult real que ele NAO bate com
    nada la. Quem casa e o `handwritten_code` ('40102', escrito a mao na
    linha), que e o scancode de verdade — ver `test_matcher.py::
    test_m2_item_code_impresso_nao_bate_mas_codigo_a_mao_sim`."""
    po_lines = [_po(1, "Beef Sirloin Top Butt", supplier_unit_id="40102",
                     ordered="3", received="209", invoiced_total_cost="1256.09")]
    items = [_item(100, "BEEF SIRLOIN TOP BUTT XT ANGUS N/R", "209.00", "6.01",
                    "1256.09", item_code="10112883", handwritten_code="40102", cases="3")]

    resultado = reconcile_items_against_po(items, po_lines, categoria_fornecedor="carne")
    (linha,) = resultado["items"]
    assert linha["match_level"] == "codigo"
    assert linha["cases_invoice"] == Decimal("3")
    assert QTY_MISMATCH_PO not in linha["issue_codes"]
    assert linha["has_issue"] is False


def test_item_peso_variavel_com_cases_diverge_quando_caixa_nao_bate():
    """Mesmo cenario, mas a invoice faturou 2 caixas onde o PO pediu 3 —
    divergencia de verdade, e a regra de cases tem que continuar acusando."""
    po_lines = [_po(1, "Beef Sirloin Top Butt", supplier_unit_id="40102",
                     ordered="3", received="140", invoiced_total_cost="841.40")]
    items = [_item(100, "BEEF SIRLOIN TOP BUTT XT ANGUS N/R", "140.00", "6.01",
                    "841.40", item_code="40102", cases="2")]

    resultado = reconcile_items_against_po(items, po_lines, categoria_fornecedor="carne")
    (linha,) = resultado["items"]
    assert QTY_MISMATCH_PO in linha["issue_codes"]
    assert linha["has_issue"] is True


def test_variante_de_estado_herda_po_da_irma_ja_casada():
    """Caso real Prime Meats/1175605 (achado do cliente, Leandro Rocha): o
    Catapult tem UMA linha de PO pra "Chicken Breast" (peito de frango), mas
    a invoice fatura em 2 linhas -- uma congelada (C031F, 15 cx / 600 lb),
    outra fresca (C105F, 10 cx / 400 lb). Sem a regra de estado, o casamento
    por nome (guloso, 1-para-1) so deixa uma reclamar o PO e a outra sobra
    'unmatched' (NO_PO_FOR_ITEM) -- mesmo com tudo certo. Com a regra: as
    duas caem no mesmo grupo, a soma (25 cx / 1000 lb / $1438.00) bate com o
    PO, e nenhuma das duas diverge."""
    po_lines = [_po(1, "Chicken Breast Boneless Skinless",
                     ordered="25", received="1000", invoiced_total_cost="1438.00",
                     receipt_alias="CHKN BREAST BL/SL DRY GEN FZN")]
    items = [
        _item(100, "CHKN BREAST BL/SL DRY GEN FZN", "600.00", "1.41", "846.00",
              cases="15"),
        _item(101, "CHKN BREAST BL/SL UNSIZED DRY GEN FRSH CVP", "400.00", "1.48", "592.00",
              cases="10"),
    ]

    resultado = reconcile_items_against_po(items, po_lines, categoria_fornecedor="carne")
    congelada, fresca = resultado["items"]
    assert congelada["id_po_item"] == 1
    assert fresca["id_po_item"] == 1
    assert congelada["match_level"] in ("codigo", "nome")
    assert fresca["match_level"] == "estado"
    assert QTY_MISMATCH_PO not in congelada["issue_codes"]
    assert QTY_MISMATCH_PO not in fresca["issue_codes"]
    assert resultado["has_issue"] is False


def test_preco_divergente_gera_price_mismatch():
    po_lines = [_po(1, "Beef Brisket Boneless", supplier_unit_id="TEU45",
                     ordered="10", received="10", invoiced_total_cost="60.10")]
    # Quantidade bate, mas o valor da linha faturada nao bate com o PO.
    items = [_item(100, "BEEF - Brisket / Maca de Peito", "10", "9.00", "90.00",
                    item_code="TEU45")]

    resultado = reconcile_items_against_po(items, po_lines)
    (linha,) = resultado["items"]
    assert PRICE_MISMATCH_PO in linha["issue_codes"]
    assert QTY_MISMATCH_PO not in linha["issue_codes"]
    assert linha["has_issue"] is True


def test_preco_usa_fallback_qtd_x_preco_unitario_sem_valor_linha():
    po_lines = [_po(1, "Beef Brisket Boneless", supplier_unit_id="TEU45",
                     ordered="10", received="10", invoiced_total_cost="60.10")]
    # total_price ausente -> cai pra 10 x 6.01 = 60.10, dentro da tolerancia.
    items = [_item(100, "BEEF - Brisket / Maca de Peito", "10", "6.01",
                    total_price=None, item_code="TEU45")]

    resultado = reconcile_items_against_po(items, po_lines)
    (linha,) = resultado["items"]
    assert PRICE_MISMATCH_PO not in linha["issue_codes"]


def test_nota_sem_po_marca_header_e_itens_sem_no_po_for_item():
    """`po_lines=[]` é a NOTA sem PO pra comparar (busca vazia, ambígua demais
    pra desempatar, ou nenhum candidato casou pelos itens -- os três chegam
    iguais aqui). Header e item ganham `PO_NAO_ENCONTRADA` (o item não pode
    virar CONFERIDO), mas o item não leva `NO_PO_FOR_ITEM`."""
    items = [_item(100, "LETTUCE ROMAINE", "1", "85.90", "85.90")]

    resultado = reconcile_items_against_po(items, po_lines=[])
    assert resultado["issue_codes"] == [PO_NAO_ENCONTRADA]
    assert resultado["has_issue"] is True

    (linha,) = resultado["items"]
    assert linha["issue_codes"] == [PO_NAO_ENCONTRADA]
    assert linha["has_issue"] is True
    assert "id_po_item" not in linha
    assert NO_PO_FOR_ITEM not in resultado["issue_codes"]


def test_po_orphan_nao_reclamado_por_nenhuma_linha_da_invoice():
    po_lines = [
        _po(1, "Beef Brisket Boneless", supplier_unit_id="TEU45",
            ordered="10", received="10", invoiced_total_cost="60.10"),
        _po(2, "Beef Tri Tip", supplier_unit_id="TEU99",
            ordered="5", received="5", invoiced_total_cost="30.00"),
    ]
    items = [_item(100, "BEEF - Brisket / Maca de Peito", "10", "6.01", "60.10",
                    item_code="TEU45")]

    resultado = reconcile_items_against_po(items, po_lines)
    assert resultado["po_orphans"] == [2]


def test_anotacao_a_mao_liga_needs_review_sem_forcar_divergencia():
    po_lines = [_po(1, "Beef Brisket Boneless", supplier_unit_id="TEU45",
                     ordered="10", received="10", invoiced_total_cost="60.10")]
    items = [_item(100, "BEEF - Brisket / Maca de Peito", "10", "6.01", "60.10",
                    item_code="TEU45", handwritten=True)]

    resultado = reconcile_items_against_po(items, po_lines)
    (linha,) = resultado["items"]
    assert linha["issue_codes"] == ["handwritten_present"]
    assert linha["needs_review"] is True
    assert linha["has_issue"] is True   # has_issue = divergente OR needs_review


def test_receipt_alias_do_po_resgata_item_com_nome_de_cadastro_fraco():
    """`receipt_alias` chega ao motor direto na `POLine` — a coluna existe na
    própria linha do PO (confirmado contra o Catapult real), sem precisar de
    ponte por catálogo. Checa que o resultado já sai pronto pra
    `save_reconciliation_items` (`id_po_item`, `price_other` preenchidos)."""
    sem_alias = [_po(1, "CHICKEN - Tender / Sassami de Frango",
                      ordered="10", received="10", invoiced_total_cost="18.50")]
    com_alias = [_po(1, "CHICKEN - Tender / Sassami de Frango",
                      ordered="10", received="10", invoiced_total_cost="18.50",
                      receipt_alias="CHIX TENDER JUMBO CVP")]
    items = [_item(100, "CHIX TENDER JUMBO CVP", "10", "1.85", "18.50")]

    resultado_sem = reconcile_items_against_po(items, sem_alias)
    (linha_sem,) = resultado_sem["items"]
    assert linha_sem["match_level"] == "unmatched"

    resultado = reconcile_items_against_po(items, com_alias)
    (linha,) = resultado["items"]
    assert linha["match_level"] == "nome"
    assert linha["id_po_item"] == 1
    assert linha["has_issue"] is False


def test_ordem_da_invoice_e_preservada():
    po_lines = [_po(1, "A", supplier_unit_id="C1"),
                _po(2, "B", supplier_unit_id="C2")]
    items = [
        _item(10, "A", "1", "1.00", "1.00", item_code="C1"),
        _item(11, "B", "1", "1.00", "1.00", item_code="C2"),
    ]

    resultado = reconcile_items_against_po(items, po_lines)
    assert [r["id_invoice_item"] for r in resultado["items"]] == [10, 11]


def test_duas_linhas_da_invoice_casam_com_o_mesmo_item_do_po():
    """Caso real (Restaurant Depot, Windermere): o mesmo produto foi passado
    em duas caixas separadas no caixa do fornecedor, e a invoice sai com 2
    linhas do mesmo UPC. O PO consolida num item so (Ordered=0 — esse
    fornecedor nao usa PO previo, so recebe direto — Received=2).

    Comparar cada linha ISOLADA contra o total do PO (2 x $21.79 == $43.58)
    sempre acusaria PRICE_MISMATCH_PO, mesmo a soma batendo exato — e o bug
    que este teste guarda. A soma das duas linhas bate com o total do PO,
    entao so QTY_MISMATCH_PO deve aparecer (por causa do Ordered=0, nao do
    preco)."""
    po_lines = [_po(1, "Egg Farm Fresh Extra Large Dozen", supplier_unit_id=None,
                     ordered="0", received="2", invoiced_total_cost="43.58")]
    items = [
        _item(10, "EGGS XLG CRT GRD A 15DZ", "1", "21.79", "21.79", upc="760695010790"),
        _item(11, "EGGS XLG CRT GRD A 15DZ", "1", "21.79", "21.79", upc="760695010790"),
    ]
    po_lines[0] = po_lines[0]._replace(scancode="760695010790")

    resultado = reconcile_items_against_po(items, po_lines)
    for linha in resultado["items"]:
        assert linha["match_level"] == "codigo"
        assert linha["id_po_item"] == 1
        assert PRICE_MISMATCH_PO not in linha["issue_codes"], \
            "preco nao pode divergir: a SOMA das duas linhas bate com o total do PO"
        assert linha["price_diff"] == Decimal("0.00")
        # Ordered=0 (fornecedor sem PO previo) diverge de verdade — nao e bug.
        assert QTY_MISMATCH_PO in linha["issue_codes"]


def test_linha_carrega_numeros_do_po_para_persistir():
    """O lado PO da comparacao tem que sair na linha, nao so o veredito —
    e o que `fat_conciliacao_item` grava (qtd_po, qtd_po_recebida, dif_qtd,
    valor_invoice, valor_po) pra auditar uma divergencia sem reabrir o
    Catapult. Faturou 8 onde o PO pediu/recebeu 10: dif_qtd = -2."""
    po_lines = [_po(1, "Beef Brisket Boneless", supplier_unit_id="TEU45",
                     ordered="10", received="10", invoiced_total_cost="60.10")]
    items = [_item(100, "BEEF - Brisket / Maca de Peito", "8", "6.01", "48.08",
                    item_code="TEU45")]

    resultado = reconcile_items_against_po(items, po_lines)
    (linha,) = resultado["items"]
    assert linha["qty_po"] == Decimal("10")
    assert linha["qty_po_received"] == Decimal("10")
    assert linha["qty_diff"] == Decimal("-2")
    assert linha["total_invoice"] == Decimal("48.08")
    assert linha["total_po"] == Decimal("60.10")


def test_dif_qtd_usa_caixa_no_peso_variavel():
    """No acougue a comparacao de quantidade e caixa x Ordered, entao a
    diferenca tambem tem que sair em CAIXA — faturou 2 onde o PO pediu 3,
    dif_qtd = -1. Subtrair `qtd - qtd_po` no SQL daria -138 (peso menos
    caixa), numero sem sentido; e por isso que a coluna e calculada aqui."""
    po_lines = [_po(1, "Beef Sirloin Top Butt", supplier_unit_id="40102",
                     ordered="3", received="140", invoiced_total_cost="841.40")]
    items = [_item(100, "BEEF SIRLOIN TOP BUTT XT ANGUS N/R", "140.00", "6.01",
                    "841.40", item_code="40102", cases="2")]

    resultado = reconcile_items_against_po(items, po_lines, categoria_fornecedor="carne")
    (linha,) = resultado["items"]
    assert linha["qty_diff"] == Decimal("-1")
    assert linha["qty_po"] == Decimal("3")


def test_cases_fora_de_carne_nao_e_comparado_contra_ordered():
    """Caso real do painel de operacao (Grafana, 'Diferenca de quantidade
    Invoices x Ordem de Compra'): itens de mercearia seca onde a Vision
    aplica o multiplicador de pack da descricao (regra 8 do prompt, ex.
    "NESTLE LEITE NINHO INTEGRAL INSTANT 12x360 GR") preenchem `cases` com a
    contagem de caixas IMPRESSA, mas o Ordered do PO esta em unidades
    individuais (mesma unidade de `quantity`, ja multiplicado) -- nao em
    caixas. Sem `categoria_fornecedor="carne"`, comparar `cases` (4) contra
    Ordered (48) dava uma divergencia de 44 mesmo com a quantidade batendo
    certinho (48 == 48)."""
    po_lines = [_po(1, "Nestle Leite Ninho Integral Instant",
                     ordered="48", received="48", invoiced_total_cost="120.00")]
    items = [_item(100, "NESTLE LEITE NINHO INTEGRAL INSTANT 12x360 GR", "48", "2.50",
                    "120.00", cases="4")]

    resultado = reconcile_items_against_po(items, po_lines)
    (linha,) = resultado["items"]
    assert linha["qty_diff"] == Decimal("0")
    assert QTY_MISMATCH_PO not in linha["issue_codes"]
    assert linha["has_issue"] is False


def test_po_single_unit_compara_quantity_multiplicada():
    """Caso real (achado do cliente, confirmado ao vivo contra o Catapult):
    'LASANHA DONA BENTA 20X500G', Qtde impressa 1, unit_price 59.99 -- a
    Vision multiplica pelo pack da descricao (regra 8) e extrai quantity=20,
    cases=1, total_price=59.99 (o total real faturado, sem recalcular). A PO
    desse item esta em 'Single Unit' (Ordered=20, na mesma unidade de
    `quantity` ja multiplicado) -- bate certinho na QUANTIDADE. O VALOR
    (`Invoiced Total Cost`) NAO acompanha esse multiplicador -- e sempre o
    total real da linha (59.99), confirmado contra dado real do Catapult:
    usar quantity x unit_price ali gerava divergencia de valor gigante e
    falsa."""
    po_lines = [_po(1, "Lasanha Dona Benta", ordered="20", received="20",
                     invoiced_total_cost="59.99", unit="Single Unit")]
    items = [_item(100, "LASANHA DONA BENTA 20X500G", "20", "59.99",
                    total_price="59.99", cases="1")]

    resultado = reconcile_items_against_po(items, po_lines)
    (linha,) = resultado["items"]
    assert linha["qty_diff"] == Decimal("0")
    assert QTY_MISMATCH_PO not in linha["issue_codes"]
    assert PRICE_MISMATCH_PO not in linha["issue_codes"]
    assert linha["has_issue"] is False


def test_po_case_compara_cases_nao_quantity_multiplicada():
    """Caso real (achado do cliente): 'Aviacao Manteiga Pote Com Sal
    24x200g', Qtde impressa 2, unit_price 165.90 -- mesmo formato de
    descricao da lasanha, mas a PO desse item esta em 'Case' (Ordered=2, a
    contagem IMPRESSA de caixas). Usar `quantity` multiplicado (48) contra
    Ordered=2 daria falso QTY_MISMATCH_PO; a funcao tem que preferir
    `cases` (2) quando `po.unit == 'Case'`, e o valor comparavel e o total
    real faturado (331.80), nao quantity(48) x unit_price."""
    po_lines = [_po(1, "Aviacao Manteiga Pote Com Sal", ordered="2", received="2",
                     invoiced_total_cost="331.80", unit="Case")]
    items = [_item(100, "Aviacao Manteiga Pote Com Sal 24x200g", "48", "165.90",
                    total_price="331.80", cases="2")]

    resultado = reconcile_items_against_po(items, po_lines)
    (linha,) = resultado["items"]
    assert linha["qty_diff"] == Decimal("0")
    assert QTY_MISMATCH_PO not in linha["issue_codes"]
    assert PRICE_MISMATCH_PO not in linha["issue_codes"]
    assert linha["has_issue"] is False


def test_linha_sem_po_nao_inventa_numero_do_lado_po():
    """Sem par no PO nao ha contra o que comparar: as colunas do lado PO
    ficam ausentes (viram NULL no banco), nunca zero."""
    po_lines = [_po(1, "Outra Coisa Totalmente Diferente", supplier_unit_id="ZZZ",
                     ordered="5", received="5", invoiced_total_cost="50.00")]
    items = [_item(100, "BEEF - Brisket / Maca de Peito", "8", "6.01", "48.08",
                    item_code="TEU45")]

    resultado = reconcile_items_against_po(items, po_lines)
    (linha,) = resultado["items"]
    assert linha["issue_codes"] == [NO_PO_FOR_ITEM]
    for chave in ("qty_po", "qty_po_received", "qty_diff", "total_invoice", "total_po"):
        assert chave not in linha


def test_separar_itens_insumo_tira_so_a_linha_marcada():
    items = [
        _item(100, "BEEF - Brisket / Maca de Peito", "10", "6.01", "60.10"),
        _item(101, "LETTUCE ROMAINE", "1", "85.90", "85.90", handwritten="Insumo"),
        _item(102, "TOMATO", "1", "5.00", "5.00", handwritten="Insuno"),
        _item(103, "ONION", "1", "5.00", "5.00", handwritten="DONE"),
    ]
    restantes, insumo = separar_itens_insumo(items)
    assert [i["id"] for i in restantes] == [100, 103]
    assert [i["id"] for i in insumo] == [101, 102]


def test_separar_itens_insumo_todas_as_linhas():
    items = [_item(100, "LETTUCE", "1", "85.90", "85.90", handwritten="Insumo")]
    assert separar_itens_insumo(items)[0] == []


def test_classificar_insumo_quando_anotacao_solta_na_pagina():
    """Caso real: Restaurant Depot/Beachline West, nota
    21146543173478892 -- 'Insumo Confeitaria' escrito na margem da pagina,
    nao preso a nenhuma linha (o Vision devolve isso em
    general_handwritten_notes, nao no handwritten_notes de nenhum item)."""
    items = [
        _item(100, "Badia Spices - Garlic Powder - 4lb Jar", "1", "125.12", "125.12"),
        _item(101, "Badia - Lemon Pepper - 6 lbs", "1", "132.61", "132.61"),
    ]
    assert classificar_anotacao_insumo("Insumo Confeitaria") == INSUMO_SIM


def test_classificar_nao_quando_anotacao_solta_nao_tem_a_palavra():
    items = [_item(100, "Badia Spices - Garlic Powder - 4lb Jar", "1", "125.12", "125.12")]
    assert classificar_anotacao_insumo("Ver com o gerente") == INSUMO_NAO
    assert classificar_anotacao_insumo(None) == INSUMO_NAO


@pytest.mark.parametrize("texto", ["IN SUMO", "Insumo Burger", "INSUMOS", "insumo", "In-Sumo"])
def test_classificar_insumo_tolera_espaco_caixa_e_complemento(texto):
    assert classificar_anotacao_insumo(texto) == INSUMO_SIM


@pytest.mark.parametrize("texto", ["INSUMO_INCERTO: FINAL WRAP", "INSUMO_INCERTO:"])
def test_classificar_marca_da_vision_sem_match_e_incerto(texto):
    assert classificar_anotacao_insumo(texto) == INSUMO_INCERTO


@pytest.mark.parametrize("texto", ["DONE", "Inverno", "J. Silva"])
def test_classificar_texto_geral_sem_marca_que_nao_bate_e_nao(texto):
    assert classificar_anotacao_insumo(texto) == INSUMO_NAO


@pytest.mark.parametrize("texto", ["insuno", "Sumo", "insmo", "Insu mo Burger",
                                   "INSUMO_INCERTO: Insumo", "INSUMO_INCERTO: Insu wm Burder"])
def test_classificar_segunda_barreira_fuzzy_acima_de_70_e_insumo(texto):
    assert classificar_anotacao_insumo(texto) == INSUMO_SIM


@pytest.mark.parametrize("texto", [None, ""])
def test_classificar_sem_texto_e_nao(texto):
    assert classificar_anotacao_insumo(texto) == INSUMO_NAO


def test_classificar_nota_de_linha_que_nao_bate_nao_gera_incerto():
    items = [_item(100, "LETTUCE ROMAINE", "1", "85.90", "85.90", handwritten="preco ok")]
    assert classificar_anotacao_insumo(None) == INSUMO_NAO


def test_classificar_insumo_vence_incerto_de_outra_linha():
    items = [_item(100, "LETTUCE ROMAINE", "1", "85.90", "85.90", handwritten="insuno")]
    assert classificar_anotacao_insumo("Insumo Burger") == INSUMO_SIM


def test_escolher_po_por_itens_candidato_que_casa_mais_itens_vence():
    items = [
        _item(100, "BEEF - Brisket / Maca de Peito", "10", "6.01", "60.10", item_code="TEU45"),
        _item(101, "Beef Tri Tip", "5", "6.00", "30.00", item_code="TEU99"),
    ]
    candidato_errado = [_po(1, "Outra Coisa", supplier_unit_id="ZZZ")]
    candidato_certo = [
        _po(1, "Beef Brisket Boneless", supplier_unit_id="TEU45"),
        _po(2, "Beef Tri Tip", supplier_unit_id="TEU99"),
    ]

    indice = escolher_po_por_itens(items, [candidato_errado, candidato_certo])
    assert indice == 1


def test_escolher_po_por_itens_none_quando_nenhum_candidato_casa():
    items = [_item(100, "LETTUCE ROMAINE", "1", "85.90", "85.90")]
    candidatos = [
        [_po(1, "Outra Coisa", supplier_unit_id="ZZZ")],
        [_po(2, "Mais Outra Coisa", supplier_unit_id="YYY")],
    ]
    assert escolher_po_por_itens(items, candidatos) is None


def test_escolher_po_por_itens_empate_desempata_por_valor():
    """Caso real (MENA/11131, 2026-09): item recorrente do fornecedor
    ('Massa Pastel Recortada', código 00130) tinha atividade real em 3 PO's
    históricas diferentes -- todas casam 100% dos itens (só há 1 item),
    empatando na fração. Escolher pela ORDEM de listagem pegaria a errada;
    o valor faturado ($239.85) só bate (~2%) com UM dos três candidatos."""
    items = [_item(100, "REI DA MASSA PASTEL DE FEIRA RECORTADA 6X2 KG", "18", "13.325",
                    "239.85", item_code="00130")]
    candidatos = [
        [_po(1, "Massa Pastel Recortada", supplier_unit_id="00130",
             invoiced_total_cost="159.90")],   # 50% de diferenca
        [_po(2, "Massa Pastel Recortada", supplier_unit_id="00130",
             invoiced_total_cost="235.00")],   # ~2% de diferenca -- o certo
        [_po(3, "Massa Pastel Recortada", supplier_unit_id="00130",
             invoiced_total_cost="165.90")],   # ~45% de diferenca
    ]

    assert escolher_po_por_itens(items, candidatos) == 1


def test_escolher_po_por_itens_empate_sem_candidato_confiavel_retorna_none():
    """Mesmo cenário do teste acima, mas nenhum dos três candidatos empatados
    bate com o valor faturado dentro da tolerância -- números reais do caso
    MENA/11131 (nenhum dos 3 batia com $239.85). Nunca escolhe por posição
    quando não há como confirmar por valor: melhor 'sem PO' (revisão manual)
    do que um match falso."""
    items = [_item(100, "REI DA MASSA PASTEL DE FEIRA RECORTADA 6X2 KG", "18", "13.325",
                    "239.85", item_code="00130")]
    candidatos = [
        [_po(1, "Massa Pastel Recortada", supplier_unit_id="00130",
             invoiced_total_cost="159.90")],
        [_po(2, "Massa Pastel Recortada", supplier_unit_id="00130",
             invoiced_total_cost="79.95")],
        [_po(3, "Massa Pastel Recortada", supplier_unit_id="00130",
             invoiced_total_cost="165.90")],
    ]

    assert escolher_po_por_itens(items, candidatos) is None


# ---------------------------------------------------------------------------
# Tolerância de preço por fornecedor
# ---------------------------------------------------------------------------

def test_tolerancia_padrao_sem_fornecedor_mapeado_tolera_um_centavo():
    po_lines = [_po(1, "Item X", supplier_unit_id="X1", ordered="1", received="1",
                     invoiced_total_cost="100.005")]
    items = [_item(100, "Item X", "1", "100.00", "100.00", item_code="X1")]
    resultado = reconcile_items_against_po(items, po_lines)
    (linha,) = resultado["items"]
    assert PRICE_MISMATCH_PO not in linha["issue_codes"]


def test_tolerancia_carne_qualquer_diferenca_e_divergencia():
    po_lines = [_po(1, "Beef Brisket", supplier_unit_id="TEU45", ordered="10", received="10",
                     invoiced_total_cost="60.099")]
    items = [_item(100, "BEEF - Brisket", "10", "6.01", "60.10", item_code="TEU45")]
    # diff = 0.001 -- toleraria no padrao (abs_tol=0.01), mas carne exige exato
    resultado = reconcile_items_against_po(items, po_lines, categoria_fornecedor="carne")
    (linha,) = resultado["items"]
    assert PRICE_MISMATCH_PO in linha["issue_codes"]


def test_tolerancia_triunfo_tolera_sub_centavo():
    po_lines = [_po(1, "Item X", supplier_unit_id="X1", ordered="1", received="1",
                     invoiced_total_cost="100.0005")]
    items = [_item(100, "Item X", "1", "100.00", "100.00", item_code="X1")]
    resultado = reconcile_items_against_po(
        items, po_lines, supplier_name="TRIUNFO FOODS IMPORT & EXPORT CORP",
    )
    (linha,) = resultado["items"]
    assert PRICE_MISMATCH_PO not in linha["issue_codes"]


def test_tolerancia_julina_diverge_a_partir_de_um_centavo():
    po_lines = [_po(1, "Item X", supplier_unit_id="X1", ordered="1", received="1",
                     invoiced_total_cost="100.01")]
    items = [_item(100, "Item X", "1", "100.00", "100.00", item_code="X1")]
    resultado = reconcile_items_against_po(items, po_lines, supplier_name="Julina Foods, Inc.")
    (linha,) = resultado["items"]
    assert PRICE_MISMATCH_PO in linha["issue_codes"]


def test_tolerancia_freshpoint_exige_valor_exato():
    po_lines = [_po(1, "Item Y", supplier_unit_id="Y1", ordered="1", received="1",
                     invoiced_total_cost="50.001")]
    items = [_item(100, "Item Y", "1", "50.00", "50.00", item_code="Y1")]
    resultado = reconcile_items_against_po(
        items, po_lines, supplier_name="Freshpoint Central FL",
    )
    (linha,) = resultado["items"]
    assert PRICE_MISMATCH_PO in linha["issue_codes"]


if __name__ == "__main__":
    falhas = 0
    testes = [(n, o) for n, o in sorted(globals().items())
              if n.startswith("test_") and callable(o)]
    for nome, func in testes:
        try:
            func()
            print(f"  ok   {nome}")
        except AssertionError as exc:
            falhas += 1
            print(f"  FALHA {nome}: {exc or 'assert'}")
        except Exception as exc:  # noqa: BLE001
            falhas += 1
            print(f"  ERRO  {nome}: {type(exc).__name__}: {exc}")
    print(f"\n{len(testes) - falhas}/{len(testes)} passaram")
    sys.exit(1 if falhas else 0)

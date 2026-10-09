"""Testes do schema de invoice (commons/vision/schema.py) -- em particular a
rede de seguranca do multiplicador de pack embutido na descricao, que
substitui a dependencia de o LLM aplicar a regra 8 do prompt de forma
consistente (achado do cliente: em notas com muitas linhas a Vision aplica
em algumas e esquece outras, mesmo com o prompt sem depender da coluna UN).

    python -m pytest tests/test_invoice_model.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from commons.vision.schema import InvoiceData, InvoiceItem  # noqa: E402


def test_multiplica_quando_vision_nao_aplicou():
    """Caso real (achado do cliente): a Vision devolveu 'Kisaber Folha de
    Louro 12x4g' com quantity=2 (a contagem impressa) e cases=None -- nao
    aplicou a regra 8 nessa linha, mesmo o prompt nao dependendo mais da
    coluna UN. O validator pega e aplica deterministicamente."""
    item = InvoiceItem(description="Kisaber Folha de Louro 12x4g", quantity=2, unit_price=13.0)
    assert item.quantity == 24
    assert item.cases == 2


def test_nao_mexe_quando_vision_ja_aplicou():
    """A Vision ja multiplicou e preencheu `cases` (regra 8 funcionou nessa
    linha) -- o validator nao deve mexer de novo (dobraria o multiplicador)."""
    item = InvoiceItem(
        description="LASANHA DONA BENTA 20X500G", quantity=20, cases=1, unit_price=59.99,
    )
    assert item.quantity == 20
    assert item.cases == 1


def test_nao_mexe_sem_padrao_na_descricao():
    """Sem '<N>x<peso>' na descricao, quantity fica como veio (caso comum:
    peso variavel de acougue, onde `quantity` ja e o peso -- ver rule 7)."""
    item = InvoiceItem(
        description="BEEF SIRLOIN TOP BUTT XT ANGUS N/R", quantity=140, cases=2, unit_price=6.01,
    )
    assert item.quantity == 140
    assert item.cases == 2

    sem_cases = InvoiceItem(
        description="BEEF SIRLOIN TOP BUTT XT ANGUS N/R", quantity=140, unit_price=6.01,
    )
    assert sem_cases.quantity == 140
    assert sem_cases.cases is None


def test_nao_multiplica_por_um():
    """'<N>' = 1 nao e multiplicador de verdade (ex. '1x500g' = uma unidade
    de 500g) -- multiplicar por 1 nao faz diferenca, mas nao deve marcar
    `cases` a toa (manteria `cases` null como se a linha nao tivesse pack)."""
    item = InvoiceItem(description="Bala Chita Mastigavel Abacaxi 1x500g", quantity=5, unit_price=10.0)
    assert item.quantity == 5
    assert item.cases is None


def test_reconhece_unidade_minuscula_e_maiuscula():
    """O padrao aparece com unidade em qualquer caixa -- minuscula (g, ml,
    kg), maiuscula (G, ML, KG) ou mista (Kg) -- confirmado contra notas
    reais de fornecedores diferentes."""
    casos = [
        ("Trufa Morango CS 90x30g", 1, 90, 1),
        ("Requeijao Cremoso Copo TRADICIONAL Tirolez 12x200g", 7, 84, 7),
        ("Queijo Tipo Mussarela Peca Tirolez 6x3.8Kg", 1, 6, 1),
        ("LACTA CHOCOLATE BIS AO LEITE 65x100.8 GR", 2, 130, 2),
        ("Aviacao Manteiga Pote Com Sal 24x200g", 2, 48, 2),
    ]
    for desc, qtd, quantity_esperada, cases_esperado in casos:
        item = InvoiceItem(description=desc, quantity=qtd, unit_price=1.0)
        assert item.quantity == quantity_esperada, desc
        assert item.cases == cases_esperado, desc


def _nota(impressa, iso):
    return InvoiceData(reading_confidence=95, invoice_date=iso, invoice_date_raw=impressa)


def test_data_mes_dia_ano_manda_sobre_o_iso_da_vision():
    """Caso real (spec-data-invoice-mdy): nota americana impressa mes/dia/ano;
    a Vision trocou dia/mes ou o ano e a nota saiu da janela da conciliacao."""
    assert _nota("10/05/26", "2026-05-10").invoice_date == "2026-10-05"
    assert _nota("10/05/2026", "2025-10-05").invoice_date == "2026-10-05"
    assert _nota("10-16-2026", None).invoice_date == "2026-10-16"


def test_primeiro_numero_maior_que_12_e_dia_mes_ano():
    assert _nota("16/10/2026", "2026-01-16").invoice_date == "2026-10-16"


def test_data_ano_primeiro_fica_como_esta():
    assert _nota("2026-10-05", "2026-10-05").invoice_date == "2026-10-05"


def test_data_impressa_ilegivel_mantem_iso_da_vision():
    assert _nota("Oct 5, 2026", "2026-10-05").invoice_date == "2026-10-05"
    assert _nota("13/45/2026", "2026-10-05").invoice_date == "2026-10-05"
    assert _nota(None, "2026-10-05").invoice_date == "2026-10-05"


def test_vencimento_tambem_converte():
    nota = InvoiceData(reading_confidence=95, due_date="2026-04-11", due_date_raw="11/04/26")
    assert nota.due_date == "2026-11-04"


def test_pack_em_coluna_separada_multiplica():
    """Caso real (spec-pack-size-coluna, Leblon 90024400): a nota imprime qtd 1 e o
    pack numa coluna propria ('12/12 oz'); a PO do Catapult conta 12 unidades. So o
    N conta -- a unidade do size (oz, lb) nao e convertida."""
    casos = [
        ("MIMOSO MINAS FRESCAL CHEESE 12oz", "12/12 oz", 1, 12),
        ("DA ROCA QUEIJO COALHO 2LBS", "6/2LB", 1, 6),
        ("DA ROCA QUEIJO COALHO 12OZ", "6/12 oz", 1, 6),
        ("ITEM QUALQUER", "6/2LBS", 2, 12),
    ]
    for desc, pack, qtd, quantity_esperada in casos:
        item = InvoiceItem(description=desc, pack_size=pack, quantity=qtd, unit_price=53.9)
        assert item.quantity == quantity_esperada, pack
        assert item.cases == qtd, pack


def test_pack_em_coluna_com_n_um_nao_multiplica():
    item = InvoiceItem(description="FARINHA", pack_size="1/40LB", quantity=3, unit_price=10.0)
    assert item.quantity == 3
    assert item.cases is None


def test_pack_em_coluna_nao_mexe_quando_cases_ja_veio():
    item = InvoiceItem(
        description="QUEIJO", pack_size="12/12 oz", quantity=12, cases=1, unit_price=53.9,
    )
    assert item.quantity == 12
    assert item.cases == 1


def test_pack_em_coluna_sem_padrao_nao_mexe():
    for pack in (None, "", "CASE", "12 oz", "12x"):
        item = InvoiceItem(description="QUEIJO", pack_size=pack, quantity=1, unit_price=5.0)
        assert item.quantity == 1 and item.cases is None, pack



def test_pack_com_quantidade_fracionada_e_peso_e_nao_multiplica():
    # spec-pack-size-coluna R7
    item = InvoiceItem(description="QUEIJO", pack_size="6/2LB", quantity=12.5, unit_price=5.0)
    assert item.quantity == 12.5 and item.cases is None


def test_invoice_number_perde_prefixo_inv():
    # spec-invoice-number-prefixo
    for bruto, esperado in [("INV-12345", "12345"), ("inv 12345", "12345"), ("12345", "12345"),
                            ("INV", "INV"), (None, None)]:
        assert InvoiceData(reading_confidence=95, invoice_number=bruto).invoice_number == esperado, bruto

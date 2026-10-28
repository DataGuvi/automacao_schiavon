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


# spec-qtd-caixa: quantos itens tem em 1 caixa/pack/display, campo isolado de
# `cases` -- nunca participa do multiplicador de `quantity`.

def test_qtd_caixa_n_x_size_na_descricao():
    casos = [
        ("Leite de Coco Sococo Tradicional 12x400g", 12),
        ("Pacoquita Santa Helena Flowpack 24x18g", 24),
        ("Feijao Carioca 10x1kg", 10),
        ("Panetone Bauducco 18x420g", 18),
        ("Trufa Morango CS 90x30g", 90),
    ]
    for desc, esperado in casos:
        item = InvoiceItem(description=desc, quantity=1, unit_price=1.0)
        assert item.qtd_caixa == esperado, desc


def test_qtd_caixa_size_x_n_invertido():
    casos = [
        ("Nutella Glass 200GR X15", 15),
        ("Pirulin Dispensador 60gr X 12", 12),
        ("Beyoglu Dubai Chocolate Single 35GR X24", 24),
    ]
    for desc, esperado in casos:
        item = InvoiceItem(description=desc, quantity=1, unit_price=1.0)
        assert item.qtd_caixa == esperado, desc


def test_qtd_caixa_n_barra_size():
    casos = [
        ("Agua Mineral 24/0.5L", 24),
        ("Creme de Leite 12/1 QT", 12),
        ("Linguica de Frango 20/1LB", 20),
        ("Starbucks Frappuccino 12/13.7 oz", 12),
    ]
    for desc, esperado in casos:
        item = InvoiceItem(description=desc, quantity=1, unit_price=1.0)
        assert item.qtd_caixa == esperado, desc
    # tambem funciona vindo da coluna pack_size, nao so da descricao
    item = InvoiceItem(description="ITEM QUALQUER", pack_size="24/0.5L", quantity=1, unit_price=1.0)
    assert item.qtd_caixa == 24


def test_qtd_caixa_pk_n():
    casos = [("PK 100", 100), ("PK 200", 200), ("PK 50", 50), ("PK 125", 125)]
    for desc, esperado in casos:
        item = InvoiceItem(description=f"Filme Plastico {desc}", quantity=1, unit_price=1.0)
        assert item.qtd_caixa == esperado, desc


def test_qtd_caixa_n_pack():
    casos = [
        ("10-Pk", 10), ("6-Pk", 6), ("20PACK", 20), ("30PACK", 30),
        ("3PACK", 3), ("4 PACK", 4), ("72PACK", 72),
    ]
    for desc, esperado in casos:
        item = InvoiceItem(description=f"Refrigerante {desc}", quantity=1, unit_price=1.0)
        assert item.qtd_caixa == esperado, desc


def test_qtd_caixa_x_n_isolado():
    casos = [("X12", 12), ("X40", 40), ("X16", 16), ("X20", 20), ("BOX X24", 24)]
    for desc, esperado in casos:
        item = InvoiceItem(description=f"Chocolate Milka {desc}", quantity=1, unit_price=1.0)
        assert item.qtd_caixa == esperado, desc


def test_qtd_caixa_roll_per_box():
    item = InvoiceItem(description="Papel para Alimentos 1 roll per box", quantity=1, unit_price=1.0)
    assert item.qtd_caixa == 1


def test_qtd_caixa_n_pcs_ou_items_soltos():
    """Formato mais comum na pratica: '<descricao> | <peso> | <N> pcs/items'
    (confirmado contra a invoice real Skyharvest 13019, replay 2026-10-28)."""
    casos = [
        ("Florbu WAFER MIX 270 g 0% Added Sugar | 270 g | 6 pcs", 6),
        ("FitSpo High Protein bar Dubai Chocolate style | 50 g | 12 pcs", 12),
        ("Quickbury Cookie selection in bag | 400 gr | 16 items", 16),
        ("MAESTRO MASSIMO Savoilardy (LADY FINGER) | 200 gm | 20 items", 20),
    ]
    for desc, esperado in casos:
        item = InvoiceItem(description=desc, quantity=1, unit_price=1.0)
        assert item.qtd_caixa == esperado, desc


def test_qtd_caixa_1_pc_sozinho_fica_null():
    """'1 pc' e material de display (shelf talker/wobbler), nao caixa de produto."""
    casos = [
        "Quickbury Sugar Free Cookies - Shelf Talker | 1 pc",
        "Maestro Massimo Italian Premium Selection - Round Shelf Wobbler | 1 pc",
    ]
    for desc in casos:
        item = InvoiceItem(description=desc, quantity=1, unit_price=0.0)
        assert item.qtd_caixa is None, desc


def test_qtd_caixa_nao_adivinha_formato_ambiguo_ou_indicador_de_estoque():
    """Codigos que a propria planilha de referencia do cliente marca como
    'provavel'/'confirmar'/'validar', ou que sao indicador de ESTOQUE (nao
    tamanho de pack), ficam null -- nunca um numero chutado."""
    ambiguos = [
        "Smirnoff Ice B24", "Blue Moon NA C24", "Cerveja 6P",
        "Salame CS 0, PC 2", "Salame CS 1, PC 0", "Refrigerante 1-Ls 24",
        "Bandeja de Espuma PK 2/100", "Tampas PK 4/58", "Saco a Vacuo PK 1000/CS",
        "Sacos Plasticos PK 20/50", "Guardanapo PK 24/250", "Detergente PK 4/1GL",
        "Sunny Foam Hanger 4/125", "Yuca Congelada 6X5 COSTA RICA (30)",
        "Frango 4/10H", "Pao de Queijo 20 Lbs Case",
    ]
    for desc in ambiguos:
        item = InvoiceItem(description=desc, quantity=1, unit_price=1.0)
        assert item.qtd_caixa is None, desc


def test_qtd_caixa_isolado_nunca_mexe_em_quantity_cases_ou_total_price():
    """Preencher `qtd_caixa` nao pode mudar nenhum outro campo -- e um campo
    isolado, independente do multiplicador de `cases`/`quantity`."""
    item = InvoiceItem(
        description="Wafer Mix 270g 6 pcs PK 100", quantity=6, unit_price=5.0, total_price=30.0,
    )
    assert item.qtd_caixa == 100
    assert item.quantity == 6
    assert item.cases is None
    assert item.total_price == 30.0


def test_qtd_caixa_ja_preenchido_pela_vision_nao_e_sobrescrito():
    item = InvoiceItem(description="PK 100", qtd_caixa=7, quantity=1, unit_price=1.0)
    assert item.qtd_caixa == 7


def test_cases_derivado_de_quantity_dividido_por_qtd_caixa_divisao_exata():
    """Caso real: invoice Skyharvest 13019, Nickelodeon Surprise Crystal Egg --
    quantity=80 (pecas faturadas), qtd_caixa=40 (pecas por caixa, "40 pcs" na
    descricao) -- divisao exata, 2 caixas."""
    item = InvoiceItem(
        description="Nickelodeon Surprise Crystal Egg | 16 g | 40 pcs", quantity=80, unit_price=1.62,
    )
    assert item.qtd_caixa == 40
    assert item.cases == 2
    assert item.quantity == 80


def test_cases_nao_derivado_quando_divisao_nao_e_exata():
    item = InvoiceItem(description="Produto | 50 pcs", quantity=50, qtd_caixa=24, unit_price=1.0)
    assert item.cases is None
    assert item.quantity == 50


def test_cases_derivado_nunca_sobrescreve_cases_ja_preenchido():
    """`cases` ja preenchido pela regra 8 (`<N>X<size>`) ou pelo acougue nunca e
    sobrescrito pela derivacao de `qtd_caixa`."""
    item = InvoiceItem(description="Produto 12x400g", quantity=3, unit_price=1.0, cases=3)
    assert item.cases == 3
    assert item.quantity == 3


def test_cases_nao_derivado_sem_qtd_caixa_ou_sem_quantity():
    sem_qtd_caixa = InvoiceItem(description="Produto qualquer", quantity=24, unit_price=1.0)
    assert sem_qtd_caixa.cases is None

    sem_quantity = InvoiceItem(description="Produto | 24 pcs", unit_price=1.0)
    assert sem_quantity.cases is None


# --- multiplicador "B24"/"C24", so Florida Distributing (spec-caixa-letra-florida-distributing) ---

def test_multiplicador_bc_so_florida_distributing():
    """Caso real: invoice Florida Distributing 200488064 -- 'HEINEKEN ... B24
    12OZ 6P' com quantity=6 impresso -> cases=6, quantity=144 (6x24),
    confirmado contra o Catapult real (Received=144)."""
    nota = InvoiceData(
        reading_confidence=90,
        supplier_name="Florida Distributing",
        items=[InvoiceItem(
            description="HEINEKEN B24 12OZ 6P", quantity=6, unit_price=38.38, total_price=230.28,
        )],
    )
    item = nota.items[0]
    assert item.cases == 6
    assert item.quantity == 144
    assert item.total_price == 230.28  # valor nunca recalculado


def test_multiplicador_bc_nao_aplica_fora_de_florida_distributing():
    """Mesmo padrao B24/C24 noutro fornecedor nao multiplica -- continua
    ambiguo (spec-qtd-caixa R3), nao reabre a regra pro resto do sistema."""
    nota = InvoiceData(
        reading_confidence=90,
        supplier_name="Sysco Central Florida",
        items=[InvoiceItem(description="CERVEJA QUALQUER B24 12OZ 6P", quantity=6, unit_price=1.0)],
    )
    item = nota.items[0]
    assert item.cases is None
    assert item.quantity == 6


def test_multiplicador_bc_nao_sobrescreve_cases_ja_preenchido():
    nota = InvoiceData(
        reading_confidence=90,
        supplier_name="Florida Distributing",
        items=[InvoiceItem(description="HEINEKEN B24 12OZ 6P", quantity=6, unit_price=1.0, cases=6)],
    )
    item = nota.items[0]
    assert item.cases == 6
    assert item.quantity == 6  # nao multiplicou de novo


def test_multiplicador_bc_nao_pega_digito_generico_so_24():
    """Achado real: 'HEINEKEN C2 5L' (barril) tem 'C2' no nome -- nao e
    tamanho de caixa, so coincidencia de formato. So '24' (valor confirmado)
    aciona o multiplicador; qualquer outro numero fica de fora."""
    nota = InvoiceData(
        reading_confidence=90,
        supplier_name="Florida Distributing",
        items=[InvoiceItem(description="HEINEKEN C2 5L", quantity=1, unit_price=38.60, total_price=38.60)],
    )
    item = nota.items[0]
    assert item.cases is None
    assert item.quantity == 1


def test_multiplicador_bc_ignora_quantidade_fracionada():
    """Quantidade fracionada e peso, nao contagem de caixas (mesmo criterio
    da regra 8)."""
    nota = InvoiceData(
        reading_confidence=90,
        supplier_name="Florida Distributing",
        items=[InvoiceItem(description="BARRIL B24 ALGO", quantity=2.5, unit_price=1.0)],
    )
    item = nota.items[0]
    assert item.cases is None
    assert item.quantity == 2.5

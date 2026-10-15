"""Testes do motor de match (conciliacao/matcher.py).

Fixtures em memoria — nao tocam o Postgres.

    python -m pytest tests/test_matcher.py -v
    python tests/test_matcher.py          (sem pytest instalado)
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from commons.matcher import (  # noqa: E402
    CONFIDENT_SCORE,
    EXACT_MIN_SCORE,
    InvoiceCodeLine,
    InvoiceLine,
    POLine,
    QuoteLine,
    compare_price,
    extract_item_code,
    grades_conflict,
    invoice_line_total,
    is_header_row,
    is_unit_priced,
    match_items,
    match_items_po,
    match_supplier,
    mesmo_item_estado_diferente,
    norm_code,
    norm_item,
    norm_supplier,
    norm_text,
    parse_price_range,
    po_items_sem_invoice,
    qty_matches_po,
    qty_matches_po_cases,
    within_tolerance,
)


# Espelha dwschiavon2.dim_item_sinonimo (fonte real: aba De-Para do Google
# Sheets, ver conciliacao/sinonimos.py). Fixture em memoria, mesmo padrao de
# ALIASES em test_resolver_fornecedor.py: antes estas 21 linhas eram o dict
# hardcoded _SINONIMOS_ITEM do matcher.
SINONIMOS = {
    "CHIX": "CHICKEN", "CHKN": "CHICKEN", "FRANGO": "CHICKEN",
    "BOI": "BEEF", "PORCO": "PORK", "SUINO": "PORK",
    "SASSAMI": "TENDER", "RABO": "OXTAIL", "ALCATRA": "SIRLOIN",
    "BARRIGA": "BELLY", "PATINHO": "KNUCKLE", "MAMINHA": "TRI TIP",
    "PICANHA": "COULOTTE", "ACEM": "CHUCK", "SOBRECOXA": "THIGHS",
    "CORACAO": "HEARTS", "MOCOTO": "FEET", "FIGADO": "LIVER",
    "BUCHO": "TRIPE", "COSTELA": "RIBS", "FRALDINHA": "FLANK",
}


def _ni(value, sinonimos=SINONIMOS):
    """norm_item com o de-para carregado — o estado normal em producao."""
    return norm_item(value, sinonimos)


def _match(invoice, quotes, **kw):
    """match_items com o de-para carregado por padrao (kw sobrescreve)."""
    kw.setdefault("sinonimos", SINONIMOS)
    return match_items(invoice, quotes, **kw)


# ---------------------------------------------------------------------------
# Normalizacao
# ---------------------------------------------------------------------------

def test_norm_text_remove_acento_pontuacao_e_nbsp():
    # O \xa0 vem do Excel e hoje esta gravado no banco.
    assert norm_text("BEEF - Picanha Prime\xa0-\xa0Multi\xa0pack - 40129") == \
        "BEEF PICANHA PRIME MULTI PACK 40129"
    assert norm_text("Maçã de Peito") == "MACA DE PEITO"
    assert norm_text(None) == ""


def test_norm_supplier_remove_sufixo_societario():
    assert norm_supplier("ZAP FOODS LLC") == "ZAP FOODS"
    assert norm_supplier("CHENEY BROTHERS, INC.") == "CHENEY BROTHERS"
    # A normalizacao sozinha NAO colapsa as duas grafias: o '(CBI)' sobra.
    # E por isso que existe supplier_alias em vez de comparar nome com nome.
    assert norm_supplier("Cheney Brothers (CBI)") == "CHENEY BROTHERS CBI"


def test_is_header_row_pega_a_linha_que_vazou_da_planilha():
    assert is_header_row("ITEM") is True
    assert is_header_row("PRICE ") is True
    assert is_header_row("BEEF - Brisket / Maca de Peito - 40122") is False


def test_norm_item_corta_a_cauda_de_catalogo_da_invoice():
    # Marca e codigo do distribuidor nao existem do lado da cotacao.
    assert _ni("CHIX TENDER JUMBO CVP - AMICK, Item #227059") == \
        "CHICKEN TENDER JUMBO CVP"
    assert _ni("BEEF OXTAIL STEER WHOLE FROZEN WHOLE - IBP, Item #204077") == \
        "BEEF OXTAIL STEER WHOLE FROZEN WHOLE"


def test_norm_item_corta_o_codigo_interno_da_cotacao():
    assert _ni("CHICKEN - Tender / Sassami de Frango - 40622") == \
        "CHICKEN TENDER TENDER DE CHICKEN"


def test_norm_item_aproxima_o_par_que_nao_casava():
    # O caso relatado: sem norm_item o par faz 46.8 e reprova em qualquer piso.
    from rapidfuzz import fuzz
    inv = "CHIX TENDER JUMBO CVP - AMICK, Item #227059"
    quo = "CHICKEN - Tender / Sassami de Frango - 40622"
    assert fuzz.token_set_ratio(norm_text(inv), norm_text(quo)) < 50
    assert fuzz.token_set_ratio(_ni(inv), _ni(quo)) > 85


def test_norm_item_sem_de_para_nao_traduz_vocabulario():
    """Sem `sinonimos`, norm_item so limpa e corta a cauda — CHIX segue CHIX."""
    assert norm_item("CHIX TENDER JUMBO CVP - AMICK, Item #227059") == \
        "CHIX TENDER JUMBO CVP"
    assert "CHICKEN" not in norm_item("CHKN HEARTS S/MTN FZN")


def test_norm_item_traduz_a_frase_inteira_nos_dois_lados():
    # DE = descricao da invoice, PARA = nome do Catapult; ambos viram o mesmo texto.
    frase = {"FINI G OVO FRITO": "FINI FRIED EGGS"}
    assert norm_item("Fini G Ovo Frito - FINI, Item #2914", frase) == "FINI FRIED EGGS"
    assert norm_item("Fini Fried Eggs - 40622", frase) == "FINI FRIED EGGS"


def test_norm_item_frase_traduzida_nao_encadeia_com_token():
    mapa = {"CHIX TENDER": "CHICKEN TENDER", "CHICKEN": "FRANGO"}
    assert norm_item("Chix Tender", mapa) == "CHICKEN TENDER"
    assert norm_item("Chicken Wings", mapa) == "FRANGO WINGS"


def test_norm_item_frase_vem_antes_do_token():
    mapa = {"CHIX": "CHICKEN", "CHIX TENDER": "TENDER STRIPS"}
    assert norm_item("Chix Tender", mapa) == "TENDER STRIPS"
    assert norm_item("Chix Wings", mapa) == "CHICKEN WINGS"


def test_frase_do_de_para_faz_a_invoice_casar_com_o_po_por_nome():
    inv = [InvoiceCodeLine(1, "Fini G Ovo Frito", None, None)]
    po = [POLine("p", None, None, "Fini Fried Eggs")]
    antes = match_items_po(inv, po)
    depois = match_items_po(inv, po, sinonimos={"FINI G OVO FRITO": "FINI FRIED EGGS"})
    assert antes[0].match_level == "unmatched"
    assert depois[0].match_level == "nome" and depois[0].match_score == 100.0


def test_peito_ambiguo_fica_fora_do_de_para():
    """'Peito de Frango' e BREAST, 'Maca de Peito' e BRISKET.

    Traduzir PEITO para um dos dois casaria o outro com o produto errado, que e
    o erro caro: a nota entra na conciliacao comparada contra o corte errado.
    """
    assert "BRISKET" not in _ni("CHICKEN - Breast Boneless / Peito de Frango")
    assert "BREAST" not in _ni("BEEF - Brisket / Maca de Peito")


def test_extract_item_code():
    assert extract_item_code("BEEF - Brisket / Maca de Peito - 40122") == "40122"
    assert extract_item_code("BEEF - Flank Steak CHOICE") is None


# ---------------------------------------------------------------------------
# Preco
# ---------------------------------------------------------------------------

def test_parse_price_range_faixa_e_valor_unico():
    assert parse_price_range("$4.95/5.35/$5.41") == (Decimal("4.95"), Decimal("5.41"))
    assert parse_price_range("$5,38") == (Decimal("5.38"), Decimal("5.38"))
    assert parse_price_range("N/A") is None
    assert parse_price_range(None) is None


def test_within_tolerance_so_diverge_se_estourar_os_dois():
    # 1 centavo em item de 2 dolares: 0,5% de diferenca, mas passa pelo abs_tol.
    assert within_tolerance(Decimal("2.00"), Decimal("2.01")) is True
    # 0,3% em item de 500: estoura o abs_tol mas passa pelo pct_tol.
    assert within_tolerance(Decimal("501.50"), Decimal("500.00")) is True
    # Estoura os dois.
    assert within_tolerance(Decimal("2.50"), Decimal("2.00")) is False


def test_compare_price_usa_a_faixa_como_tolerancia():
    dentro = compare_price(Decimal("5.35"), Decimal("4.95"), "$4.95/5.35/$5.41")
    assert dentro.status == "ok"

    acima = compare_price(Decimal("6.00"), Decimal("4.95"), "$4.95/5.35/$5.41")
    assert acima.status == "above"

    # Convencao: diff = invoice - cotacao (positivo = cobraram a mais).
    assert acima.diff == Decimal("1.05")


def test_compare_price_acima_e_abaixo():
    assert compare_price(Decimal("7.99"), Decimal("7.25")).status == "above"
    assert compare_price(Decimal("6.50"), Decimal("7.25")).status == "below"
    assert compare_price(Decimal("6.01"), Decimal("6.01")).status == "ok"


def test_is_unit_priced_separa_preco_por_libra_de_preco_por_caixa():
    """Casos reais do banco — a coluna `unit` diz 'CS' nos dois primeiros."""
    # Cheney: 3 CS x 6.01 = 18.03, mas o total e 1256.09 -> preco por libra.
    assert is_unit_priced(Decimal("3"), Decimal("6.01"), Decimal("1256.09")) is False
    # Eastern Quality: 1 CS x 4.19 = 4.19, total 338.72 -> preco por libra.
    assert is_unit_priced(Decimal("1"), Decimal("4.19"), Decimal("338.72")) is False
    # FreshPoint, alface: 1 x 85.90 = 85.90 -> preco por caixa, nao comparavel.
    assert is_unit_priced(Decimal("1"), Decimal("85.90"), Decimal("85.90")) is True
    # Prime Distribution, mercearia: 10 x 43.52 = 435.20 -> preco por caixa.
    assert is_unit_priced(Decimal("10"), Decimal("43.52"), Decimal("435.20")) is True
    # Sem dados suficientes nao ha o que afirmar.
    assert is_unit_priced(None, Decimal("4.19"), Decimal("338.72")) is False


# ---------------------------------------------------------------------------
# Grades
# ---------------------------------------------------------------------------

def test_grades_conflict_choice_x_select():
    assert grades_conflict("BEEF Coulotte Choice Single", "BEEF Coulotte 2pc (Select)") is True
    assert grades_conflict("BEEF Coulotte Choice", "BEEF Coulotte Choice Single") is False
    # Sem grade citada de um dos lados: nao ha conflito a declarar.
    assert grades_conflict("BEEF Coulotte", "BEEF Coulotte 2pc (Select)") is False


# ---------------------------------------------------------------------------
# Cascata de match
# ---------------------------------------------------------------------------

def _quote(key, name, price, raw=None):
    return QuoteLine(key, name, Decimal(str(price)), raw)


def _invoice(key, desc, price):
    return InvoiceLine(key, desc, None if price is None else Decimal(str(price)))


def test_etapa1_casa_preco_igual_como_exact():
    quotes = [
        _quote(1, "BEEF - Top Sirloin Butt / Alcatra Bola - 40102", "6.01"),
        _quote(2, "CHICKEN - Tender / Sassami de Frango - 40622", "1.85"),
    ]
    invoice = [_invoice(10, "Beef Sirloin Top Butt XT Angus N/R - Legacy Brand", "6.01")]

    (match,) = _match(invoice, quotes)
    assert match.match_level == "exact"
    assert match.quote_key == 1


def test_preco_divergente_nao_casa_na_etapa1_e_casa_na_etapa2():
    """O teste que sustenta o desenho inteiro.

    Se o item com preco errado nao casasse, a divergencia sairia como
    'sem cotacao' e ficaria invisivel — que e o motivo de o preco nao poder
    ser chave unica do par.
    """
    quotes = [_quote(1, "BEEF - Tri Tip / Maminha - 40116", "7.58")]
    invoice = [_invoice(10, "BEEF Tri Tip Maminha 40116", "9.90")]

    (match,) = _match(invoice, quotes)
    assert match.match_level == "fuzzy"
    assert match.quote_key == 1

    verdict = compare_price(Decimal("9.90"), Decimal("7.58"))
    assert verdict.status == "above"


def test_desempate_por_descricao_quando_duas_cotacoes_tem_o_mesmo_preco():
    quotes = [
        _quote(1, "PORK - Spare Ribs / Costela de Porco - 40204", "2.19"),
        _quote(2, "BEEF - Tripe / Bucho - 40104", "2.19"),
    ]
    invoice = [_invoice(10, "Pork Spare Ribs Costela de Porco", "2.19")]

    (match,) = _match(invoice, quotes)
    assert match.match_level == "exact"
    assert match.quote_key == 1


def test_preco_igual_nao_basta_para_casar_produtos_sem_relacao():
    """Caso real: os dois custavam 4.99 e o par saiu como 'coerente'.

    Chuck Roll e Knuckle sao cortes diferentes. Sem o piso de similaridade a
    Etapa 1 casava por preco e mascarava a divergencia verdadeira.
    """
    quotes = [_quote(1, "BEEF - Knuckle / Patinho - 40121", "4.99")]
    invoice = [_invoice(10, "Beef, Chuck Roll 'Neck Off' Boneless (1100)", "4.99")]

    (match,) = _match(invoice, quotes)
    assert match.match_level == "unmatched"


def test_abreviacao_e_traducao_deixam_de_ser_descricao_fraca():
    """'CHKN' x 'CHICKEN' e 'Hearts' x 'Coracao': antes 52, agora 90.

    Este par ERA o exemplo de descricao fraca casada so pelo preco. Com o
    de-para de vocabulario de `norm_item` ele passa a se sustentar no proprio
    texto, entao casa com confianca — nao precisa mais de conferencia humana.
    """
    quotes = [_quote(1, "CHICKEN - Hearts / Coracao de Frango - 40607", "1.69")]
    invoice = [_invoice(10, "CHKN HEARTS S/MTN FZN #20 4/5# CS", "1.69")]

    (match,) = _match(invoice, quotes)
    assert match.match_level == "exact"
    assert match.match_score > CONFIDENT_SCORE
    assert match.ambiguous is False


def test_match_so_sustentado_pelo_preco_vai_para_conferencia():
    """Entre EXACT_MIN_SCORE e CONFIDENT_SCORE: casa, mas com ambiguous=True.

    'St Louis Style' e 'Spare Ribs' sao cortes parentes, nao o mesmo corte — o
    par faz 66.7 e e justamente o tipo de caso que precisa de olho humano: se o
    produto estiver errado, a divergencia de preco sai como "coerente".
    """
    quotes = [_quote(1, "PORK - Spare Ribs / Costela de Porco - 40204", "2.19")]
    invoice = [_invoice(10, "PORK RIBS ST LOUIS STYLE FZN", "2.19")]

    (match,) = _match(invoice, quotes)
    assert match.match_level == "exact"
    assert EXACT_MIN_SCORE <= match.match_score < CONFIDENT_SCORE
    assert match.ambiguous is True


def test_dois_itens_da_invoice_disputando_a_mesma_cotacao():
    """Etapa 2 e 1-para-1: o perdedor tenta o proximo candidato acima do limiar."""
    quotes = [
        _quote(1, "CHICKEN - Breast Boneless / Peito de Frango s/ Osso - 40305", "1.49"),
        _quote(2, "CHICKEN - Thighs Boneless / Sobrecoxa s/ Osso - 40623", "1.94"),
    ]
    invoice = [
        _invoice(10, "CHICKEN Breast Boneless Peito de Frango s/ Osso 40305", "4.90"),
        _invoice(11, "CHICKEN Thighs Boneless Sobrecoxa s/ Osso 40623", "2.16"),
    ]

    matches = _match(invoice, quotes)
    casados = {m.invoice_key: m.quote_key for m in matches}
    assert casados[10] == 1
    assert casados[11] == 2


def test_caso_real_eastern_quality_foods():
    """Descricoes reais do banco: a cotacao nomeia o corte em duas linguas e a
    invoice usa a descricao comercial do fornecedor.

    Com o limiar 88 herdado do TASKS.md nenhum destes casava, e as tres
    divergencias sairiam como 'sem cotacao'.
    """
    quotes = [
        _quote(1, "BEEF - Feet / Mocoto - 40117", "1.69"),
        _quote(2, "BEEF - Knuckle / Patinho - 40121", "4.99"),
        _quote(3, "BEEF - Liver / Figado - 40112", "1.65"),
        _quote(4, "BEEF - Chuck Roll / Acem - 40101", "4.95"),
    ]
    invoice = [
        _invoice(10, "Beef, Feet (70100) | 56.49", "1.89"),
        _invoice(11, "Beef, Peeled Knuckle (3230) | 30.80+35.76", "4.59"),
        _invoice(12, "Beef, Liver (94710) | 30.00+31.20+33.10", "1.39"),
    ]

    casados = {m.invoice_key: m for m in _match(invoice, quotes)}
    assert casados[10].quote_key == 1
    assert casados[11].quote_key == 2
    assert casados[12].quote_key == 3
    assert all(m.match_level == "fuzzy" for m in casados.values())

    assert compare_price(Decimal("1.89"), Decimal("1.69")).status == "above"
    assert compare_price(Decimal("4.59"), Decimal("4.99")).status == "below"


def test_ordem_das_palavras_trocada_casa_no_fuzzy():
    quotes = [_quote(1, "BEEF - Brisket / Maca de Peito - 40122", "5.77")]
    invoice = [_invoice(10, "Maca de Peito / Brisket BEEF 40122", "6.50")]

    (match,) = _match(invoice, quotes)
    assert match.match_level == "fuzzy"


def test_choice_e_select_nunca_casam():
    quotes = [_quote(1, "BEEF - Coulotte 2pc (Select) / Picanha 2pc - 40123", "7.25")]
    invoice = [_invoice(10, "BEEF Coulotte Choice Picanha 2pc 40123", "7.25")]

    (match,) = _match(invoice, quotes)
    assert match.match_level == "unmatched"


def test_cotacao_com_preco_zero_nunca_entra_no_pool():
    """price = 0 significa 'nao cotado', nao 'de graca'."""
    quotes = [
        _quote(1, "BEEF - Chuck Crest / Cupim - 40111", "0"),
        _quote(2, "BEEF - Chuck Roll / Acem - 40101", "0"),
    ]
    invoice = [_invoice(10, "BEEF Chuck Crest Cupim 40111", "0")]

    (match,) = _match(invoice, quotes)
    assert match.match_level == "unmatched"
    assert match.quote_key is None


def test_item_sem_cotacao_fica_unmatched():
    quotes = [_quote(1, "BEEF - Tri Tip / Maminha - 40116", "7.58")]
    invoice = [_invoice(10, "LETTUCE ROMAINE", "85.90")]

    (match,) = _match(invoice, quotes)
    assert match.match_level == "unmatched"


def test_ordem_da_invoice_e_preservada():
    quotes = [_quote(1, "BEEF - Tri Tip / Maminha - 40116", "7.58")]
    invoice = [_invoice(10, "A", "1.00"), _invoice(11, "B", "2.00"), _invoice(12, "C", "3.00")]

    matches = _match(invoice, quotes)
    assert [m.invoice_key for m in matches] == [10, 11, 12]


def test_match_items_usa_o_de_para_recebido():
    """Mesmo par, dois de-paras: sem traducao nao casa; com traducao casa exato.

    Prova que os sinonimos vem do parametro, nao de estado do modulo.
    """
    quotes = [_quote(1, "CHICKEN - Hearts / Coracao de Frango - 40607", "1.69")]
    invoice = [_invoice(10, "CHKN HEARTS S/MTN FZN #20 4/5# CS", "1.69")]

    (sem,) = match_items(invoice, quotes, sinonimos={})
    (com,) = match_items(invoice, quotes,
                         sinonimos={"CHKN": "CHICKEN", "CORACAO": "HEARTS"})
    assert sem.match_level == "unmatched"
    assert com.match_level == "exact"


def test_match_items_sem_de_para_ainda_casa_texto_identico():
    """Ausencia de sinonimos so enfraquece: texto que ja bate continua casando."""
    quotes = [_quote(1, "BEEF BRISKET BONELESS", "5.77")]
    invoice = [_invoice(10, "BEEF BRISKET BONELESS", "6.50")]   # preco != -> Etapa 2

    (match,) = match_items(invoice, quotes, sinonimos={})
    assert match.match_level == "fuzzy"
    assert match.quote_key == 1


# ---------------------------------------------------------------------------
# Fornecedor
# ---------------------------------------------------------------------------

def test_match_supplier_sugere_cheney():
    nome, score = match_supplier("CHENEY BROTHERS, INC.", ["Cheney", "DBA Meats"])
    assert nome == "Cheney"
    assert score >= 80


def test_match_supplier_nao_sugere_fornecedor_sem_relacao():
    nome, _ = match_supplier("IMPERIAL DADE", ["Cheney", "DBA Meats"])
    assert nome is None


def test_match_supplier_separa_os_dois_prime():
    """'Prime Meats' (carne) x 'Prime Distribution USA' (mercearia).

    Com os sufixos societarios removidos o par pontua 62,5 e fica abaixo do
    limiar — mas por pouco, e so porque 'USA' foi descartado. O de-para segue
    passando por revisao humana justamente para casos assim.
    """
    nome, score = match_supplier("Prime Distribution USA", ["Prime Meats"])
    assert nome is None
    assert 50 <= score < 80

    # O fornecedor certo, esse sim, casa.
    nome, _ = match_supplier("Prime Meats", ["Prime Meats", "Cheney"])
    assert nome == "Prime Meats"

    # E as duas grafias do Cheney chegam no mesmo cadastro.
    for grafia in ("Cheney Brothers (CBI)", "CHENEY BROTHERS, INC."):
        assert match_supplier(grafia, ["Cheney", "Prime Meats"])[0] == "Cheney"


# ---------------------------------------------------------------------------
# Codigo (item_code/upc x Supplier Unit ID/scancode)
# ---------------------------------------------------------------------------

def test_norm_code_so_digitos_sem_zero_a_esquerda():
    assert norm_code("0012345") == "12345"
    assert norm_code("TEU-45") == "45"
    assert norm_code(" 123 ") == "123"
    assert norm_code(None) == ""
    assert norm_code("") == ""
    assert norm_code("ABC") == ""
    # so zeros: nao sobra digito significativo, cai pra vazio (nunca casa)
    assert norm_code("0000") == ""


# ---------------------------------------------------------------------------
# Cascata de match contra o PO (match_items_po)
# ---------------------------------------------------------------------------

def _po(key, item_name, supplier_unit_id=None, scancode=None,
        ordered=None, received=None, invoiced_total_cost=None, receipt_alias=None):
    return POLine(
        key, supplier_unit_id, scancode, item_name,
        None if ordered is None else Decimal(str(ordered)),
        None if received is None else Decimal(str(received)),
        None if invoiced_total_cost is None else Decimal(str(invoiced_total_cost)),
        receipt_alias,
    )


def _inv_code(key, desc, item_code=None, upc=None, handwritten_code=None):
    return InvoiceCodeLine(key, desc, item_code, upc, handwritten_code)


def test_m2_casa_por_item_code_exato():
    po_lines = [_po(1, "Beef Brisket Boneless", supplier_unit_id="TEU45")]
    invoice = [_inv_code(10, "BEEF - Brisket / Maca de Peito", item_code="TEU45")]

    (match,) = match_items_po(invoice, po_lines)
    assert match.match_level == "codigo"
    assert match.po_key == 1
    assert match.match_score is None
    assert match.ambiguous is False


def test_m2_casa_por_upc_com_zero_a_esquerda_e_hifen():
    po_lines = [_po(1, "Chicken Tender Jumbo", scancode="00-078900-12345")]
    invoice = [_inv_code(10, "CHIX TENDER JUMBO", upc="78900-12345")]

    (match,) = match_items_po(invoice, po_lines)
    assert match.match_level == "codigo"
    assert match.po_key == 1


def test_m2_item_code_impresso_nao_bate_mas_codigo_a_mao_sim():
    """Caso real Cheney Brothers/05-9100063327: o `item_code` IMPRESSO na
    nota (catalogo do fornecedor, ex. '10112883') nao bate com nada do
    Catapult, mas o numero escrito a mao na linha ('40102') e o scancode de
    verdade. `handwritten_code` entra como ultimo candidato de codigo."""
    po_lines = [_po(1, "Alcatra / Top Sirloin", supplier_unit_id="40102")]
    invoice = [_inv_code(10, "BEEF SIRLOIN TOP BUTT XT ANGUS N/R",
                          item_code="10112883", handwritten_code="40102")]

    (match,) = match_items_po(invoice, po_lines)
    assert match.match_level == "codigo"
    assert match.po_key == 1


def test_m2_item_code_impresso_tem_prioridade_sobre_codigo_a_mao():
    """Quando o codigo impresso JA bate com o Catapult, ele ganha — o
    escrito a mao so entra quando o impresso nao serve pra nada."""
    po_lines = [
        _po(1, "Item certo", supplier_unit_id="TEU45"),
        _po(2, "Item errado", supplier_unit_id="99999"),
    ]
    invoice = [_inv_code(10, "qualquer descricao",
                          item_code="TEU45", handwritten_code="99999")]

    (match,) = match_items_po(invoice, po_lines)
    assert match.po_key == 1


def test_m2_codigo_a_mao_que_nao_bate_com_nada_cai_pro_residuo_normal():
    """Fornecedor onde a anotacao a mao NAO e o scancode do Catapult (ex.:
    codigo da cotacao semanal) — nao acha nada em code_index e a linha
    segue pro fuzzy de nome normalmente, sem casar errado."""
    po_lines = [_po(1, "BEEF - Tri Tip / Maminha - 40116", supplier_unit_id="99999")]
    invoice = [_inv_code(10, "BEEF Tri Tip Maminha 40116", handwritten_code="12345")]

    (match,) = match_items_po(invoice, po_lines)
    assert match.match_level == "nome"
    assert match.po_key == 1


def test_sem_match_por_codigo_cai_pro_fuzzy_de_nome():
    po_lines = [_po(1, "BEEF - Tri Tip / Maminha - 40116", supplier_unit_id="99999")]
    invoice = [_inv_code(10, "BEEF Tri Tip Maminha 40116", item_code="TEU45")]

    (match,) = match_items_po(invoice, po_lines)
    assert match.match_level == "nome"
    assert match.po_key == 1


def test_item_sem_match_nenhum_fica_unmatched():
    po_lines = [_po(1, "BEEF - Tri Tip / Maminha - 40116", supplier_unit_id="99999")]
    invoice = [_inv_code(10, "LETTUCE ROMAINE", item_code="TEU45")]

    (match,) = match_items_po(invoice, po_lines)
    assert match.match_level == "unmatched"
    assert match.po_key is None


def test_desempate_1_para_1_no_fuzzy_de_nome():
    """Dois itens da invoice disputando o mesmo item do PO por nome: o
    perdedor tenta o proximo candidato acima do limiar — mesma regra de
    `match_items`."""
    po_lines = [
        _po(1, "CHICKEN - Breast Boneless / Peito de Frango s/ Osso - 40305"),
        _po(2, "CHICKEN - Thighs Boneless / Sobrecoxa s/ Osso - 40623"),
    ]
    invoice = [
        _inv_code(10, "CHICKEN Breast Boneless Peito de Frango s/ Osso 40305"),
        _inv_code(11, "CHICKEN Thighs Boneless Sobrecoxa s/ Osso 40623"),
    ]

    matches = match_items_po(invoice, po_lines)
    casados = {m.invoice_key: m.po_key for m in matches}
    assert casados[10] == 1
    assert casados[11] == 2


def test_item_do_po_ja_casado_por_codigo_nao_entra_no_fuzzy():
    """Um item do PO reclamado na Etapa 1 (codigo) sai do pool da Etapa 2
    (nome) — nao pode ser reclamado duas vezes."""
    po_lines = [
        _po(1, "BEEF - Tri Tip / Maminha - 40116", supplier_unit_id="TEU45"),
    ]
    invoice = [
        _inv_code(10, "BEEF Tri Tip Maminha 40116", item_code="TEU45"),
        _inv_code(11, "BEEF Tri Tip Maminha 40116"),  # mesma descricao, sem codigo
    ]

    matches = match_items_po(invoice, po_lines)
    casados = {m.invoice_key: m for m in matches}
    assert casados[10].match_level == "codigo"
    assert casados[11].match_level == "unmatched"


def test_ordem_da_invoice_e_preservada_no_match_po():
    po_lines = [_po(1, "BEEF - Tri Tip / Maminha - 40116", supplier_unit_id="TEU45")]
    invoice = [_inv_code(10, "A"), _inv_code(11, "B"), _inv_code(12, "C")]

    matches = match_items_po(invoice, po_lines)
    assert [m.invoice_key for m in matches] == [10, 11, 12]


def test_po_items_sem_invoice_reverse_check():
    po_lines = [
        _po(1, "BEEF - Tri Tip / Maminha - 40116", supplier_unit_id="TEU45"),
        _po(2, "BEEF - Brisket / Maca de Peito", supplier_unit_id="TEU99"),
    ]
    invoice = [_inv_code(10, "BEEF Tri Tip Maminha 40116", item_code="TEU45")]

    matches = match_items_po(invoice, po_lines)
    assert po_items_sem_invoice(po_lines, matches) == [2]


# ---------------------------------------------------------------------------
# receipt_alias do proprio PO como alvo alternativo do fuzzy de nome
#
# Confirmado contra o Catapult real (Windermere, 2026-02): a grade Items do
# PO tem a coluna "Receipt Alias" na propria linha — nao precisa de uma ponte
# separada por dim_item_catapult pra resgatar item cujo nome de cadastro
# (item_name) nao bate com a descricao da invoice.
# ---------------------------------------------------------------------------

# PO item_name "de cadastro" — Catapult mistura idioma/formato interno, o
# mesmo tipo de string que ja fazia `norm_item` necessario pro lado cotacao.
# Sem sinonimos, 'CHIX TENDER JUMBO CVP' x isto fica em 49 (abaixo do piso de
# 70) — o fuzzy contra item_name sozinho genuinamente falha.
_PO_NOME_CADASTRO = "CHICKEN - Tender / Sassami de Frango"
_RECEIPT_ALIAS = "CHIX TENDER JUMBO CVP"   # grafia da invoice, na propria linha do PO


def test_receipt_alias_resgata_item_com_nome_de_cadastro_fraco():
    """O caso real: a descricao da invoice nao se aproxima o suficiente do
    `item_name` de cadastro do PO, mas bate cheio com o `receipt_alias` da
    MESMA linha."""
    po_lines = [_po(1, _PO_NOME_CADASTRO, receipt_alias=_RECEIPT_ALIAS)]
    invoice = [_inv_code(10, _RECEIPT_ALIAS)]

    # Prova que so o item_name nao bastaria — isola o ganho do receipt_alias.
    (so_item_name,) = match_items_po(invoice, [_po(1, _PO_NOME_CADASTRO)])
    assert so_item_name.match_level == "unmatched"

    (match,) = match_items_po(invoice, po_lines)
    assert match.match_level == "nome"
    assert match.po_key == 1


def test_po_sem_receipt_alias_ainda_casa_por_item_name():
    """`receipt_alias=None` (o padrao) preserva o comportamento anterior —
    quem ainda nao tem essa coluna preenchida nao perde o match por nome."""
    po_lines = [_po(1, "BEEF - Tri Tip / Maminha - 40116")]
    invoice = [_inv_code(10, "BEEF Tri Tip Maminha 40116")]

    (match,) = match_items_po(invoice, po_lines)
    assert match.match_level == "nome"


def test_desempate_1_para_1_quando_dois_itens_batem_no_mesmo_receipt_alias():
    """Dois itens da invoice resolvem pro MESMO item do PO — aqui empatados
    em score (`token_set_ratio` da 100 pros dois porque um e subconjunto do
    outro, propriedade ja documentada no topo do modulo). O desempate e
    guloso: o primeiro da lista fica com o par, o outro fica sem candidato e
    vira 'unmatched'."""
    po_lines = [_po(1, _PO_NOME_CADASTRO, receipt_alias=_RECEIPT_ALIAS)]
    invoice = [
        _inv_code(10, _RECEIPT_ALIAS),
        _inv_code(11, _RECEIPT_ALIAS + " EXTRA"),
    ]

    matches = {m.invoice_key: m for m in match_items_po(invoice, po_lines)}
    vencedores = [k for k, m in matches.items() if m.match_level == "nome"]
    assert vencedores == [10]
    assert matches[11].match_level == "unmatched"


# ---------------------------------------------------------------------------
# Comparacao qty/preco contra o PO
# ---------------------------------------------------------------------------

def test_qty_matches_po_as_tres_batem():
    assert qty_matches_po(Decimal("10"), Decimal("10"), Decimal("10")) is True
    # 1 centavo... quantidade usa a mesma tolerancia (abs 0.01 / pct 0.5%).
    assert qty_matches_po(Decimal("200"), Decimal("201"), Decimal("200")) is True


def test_qty_matches_po_diverge():
    assert qty_matches_po(Decimal("10"), Decimal("8"), Decimal("8")) is False
    assert qty_matches_po(Decimal("10"), Decimal("10"), Decimal("7")) is False


def test_qty_matches_po_falta_de_dado_nao_bate():
    assert qty_matches_po(None, Decimal("10"), Decimal("10")) is False
    assert qty_matches_po(Decimal("10"), None, Decimal("10")) is False


def test_qty_matches_po_cases_caixa_bate_mesmo_com_peso_variavel():
    """Caso real Cheney Brothers/05-9100063327: Ordered=3 caixas, Received=209
    lb (peso pesado na doca). Caixa da invoice bate com Ordered -> OK, mesmo
    o peso (209) nunca podendo bater com uma contagem de caixa."""
    assert qty_matches_po_cases(Decimal("3"), Decimal("3")) is True


def test_qty_matches_po_cases_diverge():
    assert qty_matches_po_cases(Decimal("2"), Decimal("3")) is False


def test_qty_matches_po_cases_falta_de_dado_nao_bate():
    assert qty_matches_po_cases(None, Decimal("3")) is False
    assert qty_matches_po_cases(Decimal("3"), None) is False


def test_mesmo_item_estado_diferente_frango_fresco_e_congelado():
    """Caso real Prime Meats/1175605 (achado do cliente, Leandro Rocha): o
    lado congelado, sem UNSIZED/CVP, fica um subconjunto exato do lado
    fresco depois de tirar FZN/FRSH -> token_set_ratio = 100."""
    assert mesmo_item_estado_diferente(
        "CHKN BREAST BL/SL DRY GEN FZN",
        "CHKN BREAST BL/SL UNSIZED DRY GEN FRSH CVP",
    ) is True


def test_mesmo_item_estado_diferente_produtos_realmente_distintos_nao_funde():
    assert mesmo_item_estado_diferente(
        "CHKN BREAST BL/SL DRY GEN FZN",
        "BEEF OXTAIL STEER WHOLE FROZEN WHOLE",
    ) is False


def test_mesmo_item_estado_diferente_bloqueia_grade_conflitante():
    assert mesmo_item_estado_diferente(
        "BEEF Coulotte 2pc (Select) FRZN",
        "BEEF Coulotte 2pc (Choice) FRSH",
    ) is False


def test_mesmo_item_estado_diferente_falta_de_descricao_nao_funde():
    assert mesmo_item_estado_diferente(None, "CHKN BREAST BL/SL FZN") is False


def test_invoice_line_total_usa_valor_linha_ou_cai_pro_fallback():
    assert invoice_line_total(Decimal("3"), Decimal("6.01"), Decimal("18.03")) == Decimal("18.03")
    assert invoice_line_total(Decimal("3"), Decimal("6.01"), None) == Decimal("18.03")
    assert invoice_line_total(None, Decimal("6.01"), None) is None


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

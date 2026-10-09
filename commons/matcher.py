"""Motor de match e comparacao — normalizacao, tolerancia e casamento de itens.

Este modulo NAO importa nada do projeto (so stdlib + rapidfuzz), de proposito:
assim ele e testavel sem Postgres e pode ser reaproveitado pela frente ERP. O
de-para de vocabulario de item (antes o dict `_SINONIMOS_ITEM`) entra por
parametro (`sinonimos`), carregado do banco pelo chamador.

A cascata de match esta em `match_items()`; a justificativa de por que o preco
nao pode ser chave unica do par esta no docstring de la.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, NamedTuple, Sequence

from rapidfuzz import fuzz

# Tolerancia padrao: so e divergencia se estourar os DOIS limites.
ABS_TOL = Decimal("0.01")
PCT_TOL = Decimal("0.005")

# -----------------------------------------------------------------------------
# Pisos de similaridade
#
# Os tres numeros abaixo foram RECALIBRADOS junto com `norm_item()`. Antes dele
# a comparacao era feita sobre o texto cru, onde marca, codigo de catalogo e o
# nome em portugues diluiam o score; os pisos antigos (60/50/70) refletiam essa
# nuvem achatada — o comentario original registrava "o melhor par CORRETO
# observado faz 69".
#
# Com `norm_item()`, medido sobre os 4 pares corretos e 5 pares errados que
# existem em base, as duas nuvens se separam:
#
#     pares corretos   88.0 .. 91.9
#     pares errados    24.6 .. 50.0   (o pior e Chuck Roll x Knuckle, 50.0)
#
# Vao de 38 pontos. Os pisos ficam dentro dele, longe das duas bordas.
#
# ATENCAO ao mexer: sao 9 pares medidos, amostra pequena. Subir demais volta a
# perder par correto em silencio (sai como 'no_quote_for_item'); descer demais
# ressuscita o falso positivo por preco que o teste
# `test_preco_igual_nao_basta_para_casar_produtos_sem_relacao` guarda.
# Qualquer mudanca aqui deve ser medida de novo, nao estimada.
# -----------------------------------------------------------------------------

# Etapa 2 — a descricao decide SOZINHA, sem apoio do preco. Piso mais alto que
# o da Etapa 1 justamente por isso.
#
# token_set_ratio no lugar de token_sort_ratio: a invoice carrega marca e pack
# size a mais, e o sort_ratio pune diferenca de tamanho.
FUZZY_THRESHOLD = 70.0

# Etapa 1 — o preco ja bate, entao a descricao so precisa dar apoio minimo ao
# par. Sem este piso, dois produtos sem relacao casam so por custarem o mesmo:
# "Beef, Chuck Roll 'Neck Off' Boneless" casou com "BEEF - Knuckle / Patinho"
# porque ambos custavam 4.99 e era o unico candidato.
EXACT_MIN_SCORE = 65.0

# Acima do piso mas abaixo disto, o par se sustenta em pouca evidencia textual.
# Aceita, mas marca para conferencia humana: se o produto estiver errado, a
# divergencia verdadeira passa despercebida como "coerente".
#
# Mantido em 70 de proposito. O corpus nao sustenta um valor mais alto: tirando
# os pares que `grades_conflict` ja barra antes do score, o pior par CORRETO faz
# 88.0 e o pior par ERRADO faz 86.8 ('CHICKEN Breast Boneless' x 'CHICKEN
# Thighs Boneless', que so diferem no corte). As nuvens quase se tocam, entao
# nao existe fronteira de confianca defensavel nessa faixa — quem separa esse
# par e a atribuicao 1-para-1 gulosa da Etapa 2, nao o piso.
CONFIDENT_SCORE = 70.0

# Sufixos societarios removidos ao normalizar nome de fornecedor.
_COMPANY_SUFFIXES = frozenset({
    "INC", "LLC", "LLP", "LP", "CORP", "CORPORATION", "CO", "COMPANY",
    "LTD", "LTDA", "SA", "SL", "GROUP", "USA",
})

# Grades de carne que nunca podem casar entre si.
_GRADES = ("CHOICE", "SELECT", "PRIME", "ANGUS", "WAGYU")

# Rotulos de cabecalho que vazaram da planilha para dentro dos dados.
HEADER_LABELS = frozenset({"ITEM", "PRICE", "COMMENTS", "PRECO", "PRODUTO"})

# Cauda de catalogo da invoice: ' - MARCA, Item #NNNNN' no fim da descricao.
# Padrao estrutural, presente nas quatro descricoes reais em base.
_CAUDA_INVOICE_RE = re.compile(r"\s*-\s*[^,]+,\s*ITEM\s*#?\s*\d+\s*$", re.IGNORECASE)

# Codigo interno no fim da linha de cotacao: ' - 40622'.
_CAUDA_COTACAO_RE = re.compile(r"\s*-\s*\d{4,6}\s*$")

# De-para de vocabulario entre os dois lados (abreviacao de mercado, traducao
# PT/EN, sinonimo de corte). ERA um dict hardcoded aqui; virou dado de banco
# (dwschiavon2.dim_item_sinonimo), injetado via parametro `sinonimos` para nao
# quebrar a regra de este modulo nao importar nada do projeto. O de-para vive
# na aba De-Para de uma planilha do Google Sheets (conciliacao/sinonimos.py);
# as entradas iniciais foram migradas em manutencao/migrar_sinonimos_sheets.py.
#
# Contrato: mapa {token_normalizado -> termo alvo}, aplicado token a token aos
# DOIS lados. Ausencia de sinonimos so enfraquece o match (errar para menos e
# seguro); nunca casa produto errado.

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_ITEM_CODE_RE = re.compile(r"-\s*(\d{4,6})\s*$")


# ---------------------------------------------------------------------------
# Normalizacao
# ---------------------------------------------------------------------------

def norm_text(value: str | None) -> str:
    """Maiusculas, sem acento, sem pontuacao, espacos colapsados.

    Trata o espaco nao-separavel (U+00A0) que aparece nos nomes vindos do Excel.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("\xa0", " ").upper()
    text = re.sub(r"[^0-9A-Z]+", " ", text)
    return " ".join(text.split())


def norm_supplier(value: str | None) -> str:
    """`norm_text` + remocao dos sufixos societarios (INC, LLC, CORP...)."""
    tokens = [t for t in norm_text(value).split() if t not in _COMPANY_SUFFIXES]
    return " ".join(tokens)


def norm_item(value: str | None, sinonimos: Mapping[str, str] | None = None) -> str:
    """Normaliza descricao de item para COMPARACAO entre os dois lados.

    `norm_text` sozinho nao basta porque os dois lados falam linguas diferentes
    sobre o mesmo produto. Medido nos 4 itens reais em base, so 1 dos 4 pares
    corretos alcancava o piso de 60; com os dois passos abaixo, os 4 passam de
    88, com margem de +41 a +58 sobre o segundo colocado.

    Passo 1 — corta a cauda de catalogo. E estrutura, nao vocabulario: a marca
    e o codigo interno do distribuidor ('- AMICK, Item #227059') nao existem do
    lado da cotacao, entao so diluem o score.

    Passo 2 — aplica o de-para de vocabulario `sinonimos` (mapa
    {token -> termo alvo}, vem de dim_item_sinonimo). Sem ele, o unico token em
    comum entre 'CHIX TENDER JUMBO CVP' e 'CHICKEN - Tender / Sassami de Frango'
    e 'TENDER', e o par correto faz 46. `sinonimos=None` -> Passo 2 nao traduz
    (degrada com seguranca).

    NAO usar para exibir nome ao usuario: o resultado e texto de comparacao.
    """
    de_para = sinonimos or {}
    texto = _CAUDA_INVOICE_RE.sub("", value or "")
    texto = _CAUDA_COTACAO_RE.sub("", texto)
    return " ".join(de_para.get(t, t) for t in norm_text(texto).split())


def is_header_row(item_name: str | None) -> bool:
    """True para a linha de cabecalho da planilha que virou dado (item_name='ITEM')."""
    return norm_text(item_name) in HEADER_LABELS


def extract_item_code(item_name: str | None) -> str | None:
    """Codigo numerico ao final do nome do item ('... - 40122' -> '40122')."""
    if not item_name:
        return None
    match = _ITEM_CODE_RE.search(str(item_name).replace("\xa0", " ").strip())
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Preco
# ---------------------------------------------------------------------------

def parse_price_range(price_raw: str | None) -> tuple[Decimal, Decimal] | None:
    """Faixa de preco a partir do texto original da celula.

    '$4.95/5.35/$5.41' -> (4.95, 5.41)      '$5,38' -> (5.38, 5.38)
    'N/A' / '' / None  -> None

    O fornecedor as vezes cota uma faixa em vez de um valor unico; nesse caso a
    faixa inteira e considerada dentro do combinado.
    """
    if price_raw is None:
        return None
    cleaned = str(price_raw).strip().replace("$", "").replace(",", ".")
    if not cleaned or cleaned.upper() in ("N/A", "NA", "-"):
        return None
    values: list[Decimal] = []
    for token in _NUMBER_RE.findall(cleaned):
        try:
            values.append(Decimal(token))
        except InvalidOperation:
            continue
    if not values:
        return None
    return min(values), max(values)


def within_tolerance(
    a: Decimal | float | None,
    b: Decimal | float | None,
    abs_tol: Decimal = ABS_TOL,
    pct_tol: Decimal = PCT_TOL,
) -> bool:
    """True se `a` e `b` sao considerados iguais.

    So diverge se estourar os DOIS limites: uma diferenca de 1 centavo num item
    de 2 dolares e percentualmente grande mas irrelevante, e 0,3% num item de
    500 dolares e o arredondamento de sempre.
    """
    if a is None or b is None:
        return False
    a, b = Decimal(str(a)), Decimal(str(b))
    diff = abs(a - b)
    if diff <= abs_tol:
        return True
    if b == 0:
        return False
    return (diff / abs(b)) <= pct_tol


class PriceVerdict(NamedTuple):
    status: str                  # 'ok' | 'above' | 'below'
    diff: Decimal | None         # invoice - cotacao
    diff_pct: Decimal | None     # em %


def is_unit_priced(
    quantity: Decimal | float | None,
    unit_price: Decimal | float | None,
    total_price: Decimal | float | None,
    rel_tol: Decimal = Decimal("0.01"),
) -> bool:
    """True se a linha esta precificada por unidade/caixa, e nao por libra.

    A cotacao e sempre por LB. Na invoice de carne o `unit_price` tambem e por
    LB, mesmo com `unit='CS'`: o total fecha com o peso, nao com a quantidade
    de caixas (3 CS x 6.01 != 1256.09, mas 209.00 lb x 6.01 == 1256.09).

    Numa linha vendida por caixa ou unidade, ao contrario, quantidade x preco
    fecha o total (1 x 85.90 == 85.90 de uma caixa de alface). Essa linha nao e
    comparavel com um preco por libra — daí `unit_mismatch`.

    A coluna `unit` nao serve para isso: esta suja ('CASE', 'case', 'CS', 'CX',
    '25#') e nula em quase 10% dos itens.
    """
    if quantity is None or unit_price is None or total_price is None:
        return False
    qty = Decimal(str(quantity))
    price = Decimal(str(unit_price))
    total = Decimal(str(total_price))
    if total == 0:
        return False
    esperado = qty * price
    return abs(esperado - total) <= abs(total) * rel_tol


def compare_price(
    invoice_price: Decimal | float | None,
    quote_price: Decimal | float | None,
    price_raw: str | None = None,
    abs_tol: Decimal = ABS_TOL,
    pct_tol: Decimal = PCT_TOL,
) -> PriceVerdict:
    """Compara o preco faturado com o cotado, respeitando faixa e tolerancia.

    Convencao do projeto: `diff = invoice - cotacao` (positivo = cobraram a mais).
    O percentual e sempre relativo ao preco principal da cotacao.
    """
    if invoice_price is None or quote_price is None:
        return PriceVerdict("ok", None, None)

    invoice = Decimal(str(invoice_price))
    quote = Decimal(str(quote_price))

    diff = invoice - quote
    diff_pct = (diff / quote * 100) if quote else None

    low, high = quote, quote
    band = parse_price_range(price_raw)
    if band is not None:
        low, high = min(band[0], quote), max(band[1], quote)

    if low <= invoice <= high:
        return PriceVerdict("ok", diff, diff_pct)
    if within_tolerance(invoice, high, abs_tol, pct_tol) or \
            within_tolerance(invoice, low, abs_tol, pct_tol):
        return PriceVerdict("ok", diff, diff_pct)
    return PriceVerdict("above" if invoice > high else "below", diff, diff_pct)


# ---------------------------------------------------------------------------
# Grades de carne
# ---------------------------------------------------------------------------

def grades_in(text: str | None) -> frozenset[str]:
    """Grades citadas no texto ('BEEF Coulotte 2pc (Select)' -> {'SELECT'})."""
    tokens = set(norm_text(text).split())
    return frozenset(g for g in _GRADES if g in tokens)


def grades_conflict(a: str | None, b: str | None) -> bool:
    """True se os dois lados citam grades e elas nao tem nada em comum.

    CHOICE e SELECT sao cortes diferentes com precos diferentes; casa-los
    produziria uma divergencia que nao existe.
    """
    ga, gb = grades_in(a), grades_in(b)
    return bool(ga and gb and not (ga & gb))


# ---------------------------------------------------------------------------
# Variante de estado (fresco x congelado) da MESMA linha de invoice
# ---------------------------------------------------------------------------

# Fresco x congelado e forma de entrega, nao identidade do produto — fica
# FORA do de-para de sinonimos (`sinonimos`, vocabulario cotacao x invoice
# compartilhado com reconcile_quote.py) de proposito: e regra especifica de
# agrupar duas linhas da MESMA invoice, nao traducao de vocabulario.
_PALAVRAS_ESTADO = frozenset({"FRESH", "FRSH", "FROZEN", "FRZN", "FZN", "FRZ"})

# Piso alto de proposito: aqui a acao e SOMAR duas linhas da invoice num so
# grupo pra comparar com o PO — mais consequente que so marcar "precisa
# revisao" (o que os pisos de match_items_po fazem), entao pede muito mais
# evidencia textual que o CONFIDENT_SCORE (70) daquele match.
MESMO_ITEM_ESTADO_DIFERENTE_SCORE = 95.0


def _sem_palavras_estado(texto_normalizado: str) -> str:
    return " ".join(t for t in texto_normalizado.split() if t not in _PALAVRAS_ESTADO)


def mesmo_item_estado_diferente(
    desc_a: str | None,
    desc_b: str | None,
    sinonimos: Mapping[str, str] | None = None,
    score_cutoff: float = MESMO_ITEM_ESTADO_DIFERENTE_SCORE,
) -> bool:
    """True se `desc_a` e `desc_b` sao o MESMO produto entregue em estado
    diferente (fresco x congelado) — caso real Prime Meats/1175605 (achado
    do cliente, Leandro Rocha): 'CHKN BREAST BL/SL DRY GEN FZN' e 'CHKN
    BREAST BL/SL UNSIZED DRY GEN FRSH CVP' sao o mesmo peito de frango pra
    quem recebe — só uma parte do pedido veio congelada e a outra fresca.

    Remove as palavras de estado antes de pontuar. `token_set_ratio` tolera
    bem token extra de UM lado so, e e exatamente esse o formato do par
    acima depois de tirar FZN/FRSH: o lado congelado (sem UNSIZED/CVP) fica
    um SUBCONJUNTO exato do lado fresco, o que pontua 100.

    Bloqueia grade de carne conflitante (`grades_conflict`) — nunca funde
    CHOICE com SELECT so por coincidencia de score. Usado so pra decidir se
    duas linhas da invoice sao a MESMA linha de PO, nao pra casar contra
    PO/cotacao (lá o estado pode ser informativo) — ver
    `conciliacao/reconcile_erp.py`.
    """
    if grades_conflict(desc_a, desc_b):
        return False
    a = _sem_palavras_estado(norm_item(desc_a, sinonimos))
    b = _sem_palavras_estado(norm_item(desc_b, sinonimos))
    if not a or not b:
        return False
    return fuzz.token_set_ratio(a, b, score_cutoff=score_cutoff) >= score_cutoff


# ---------------------------------------------------------------------------
# Match de itens
# ---------------------------------------------------------------------------

class InvoiceLine(NamedTuple):
    key: Any                       # invoice_items.id
    description: str | None
    unit_price: Decimal | None


class QuoteLine(NamedTuple):
    key: Any                       # price_quote.id
    item_name: str | None
    price: Decimal
    price_raw: str | None = None


class ItemMatch(NamedTuple):
    invoice_key: Any
    quote_key: Any | None
    match_level: str               # 'exact' | 'fuzzy' | 'unmatched'
    match_score: float | None
    ambiguous: bool = False        # empate no desempate -> conferencia humana


def match_supplier(
    name: str | None,
    candidates: Sequence[str],
    threshold: float = 80.0,
) -> tuple[str | None, float]:
    """Melhor candidato para um nome de fornecedor, por `token_set_ratio`.

    `token_set_ratio` porque a razao social da invoice costuma ser um
    superconjunto do nome curto do cadastro ('CHENEY BROTHERS, INC.' / 'Cheney').

    ATENCAO: usar so para SUGERIR de-para para revisao humana, nunca para
    decidir sozinho — 'Prime Meats' (carne) e 'Prime Distribution USA'
    (mercearia) pontuam alto e sao fornecedores diferentes.
    """
    target = norm_supplier(name)
    if not target or not candidates:
        return None, 0.0
    best, best_score = None, 0.0
    for candidate in candidates:
        score = fuzz.token_set_ratio(target, norm_supplier(candidate))
        if score > best_score:
            best, best_score = candidate, score
    return (best, best_score) if best_score >= threshold else (None, best_score)


# Piso para aceitar variacao de escrita (typo/abreviacao) sem subconjunto de tokens.
_PISO_NOME_CATAPULT = 85.0

# Palavras que nao identificam o fornecedor: sozinhas nao bastam para o subconjunto de tokens.
_TOKENS_GENERICOS = frozenset({
    "food", "foods", "distribution", "distributor", "distributors", "distributing",
    "supply", "supplies", "wholesale", "imports", "import", "usa", "us", "trading",
    "group", "company", "co", "international", "products", "produce",
})


def pontuar_nome_catapult(nome: str | None, prefixo: str | None) -> float:
    """Similaridade 0-100 entre o nome buscado e um prefixo de Name de PO.

    100 quando um e igual ao outro ou os tokens de um estao contidos nos do outro
    ('Leblon foods' x 'Leblon'): palavra a mais ou a menos nao e diferenca, desde
    que o menor tenha um token que identifique (nao 'foods' sozinho). Senao
    o melhor entre `token_sort_ratio` e a razao dos textos sem espaco (cobre
    'Fresh Poin' x 'Freshpoint'). Um token em comum entre dois nomes com tokens
    distintos ('Prime Meats' x 'Prime Distribution') NAO chega ao piso.
    """
    a, b = norm_supplier(nome), norm_supplier(prefixo)
    if not a or not b:
        return 0.0
    ta, tb = set(a.lower().split()), set(b.lower().split())
    if (ta <= tb or tb <= ta) and (ta if len(ta) <= len(tb) else tb) - _TOKENS_GENERICOS:
        return 100.0
    return max(
        fuzz.token_sort_ratio(a, b),
        fuzz.ratio(a.replace(" ", ""), b.replace(" ", "")),
    )


def aceitar_prefixos_catapult(
    nomes: Sequence[str | None], prefixos: Sequence[str], piso: float = _PISO_NOME_CATAPULT,
) -> tuple[list[str], dict[str, float]]:
    """Prefixos de PO que sao confiavelmente o mesmo fornecedor de `nomes`.

    Devolve `(aceitos, scores)`; `scores` = melhor pontuacao de cada prefixo
    distinto (para diagnostico em log). Se algum prefixo e IGUAL (normalizado) a
    um dos nomes, so os iguais valem ('Prime' nao disputa com 'Prime Meats').
    Mais de um aceito sem igualdade = ambiguo: quem chama desempata por itens e
    valor, nunca pelo score. Lista vazia = sem correspondencia confiavel.
    """
    distintos = list(dict.fromkeys(p for p in prefixos if p))
    scores = {p: max((pontuar_nome_catapult(n, p) for n in nomes), default=0.0) for p in distintos}
    alvos = {norm_supplier(n) for n in nomes if norm_supplier(n)}
    exatos = [p for p in distintos if norm_supplier(p) in alvos]
    if exatos:
        return exatos, scores
    return [p for p in distintos if scores[p] >= piso], scores


def match_items(
    invoice_lines: Sequence[InvoiceLine],
    quote_lines: Sequence[QuoteLine],
    fuzzy_threshold: float = FUZZY_THRESHOLD,
    abs_tol: Decimal = ABS_TOL,
    pct_tol: Decimal = PCT_TOL,
    *,
    sinonimos: Mapping[str, str] | None = None,
) -> list[ItemMatch]:
    """Casa itens da invoice com itens da cotacao, em cascata.

    Etapa 1 — assinatura de preco. Casa o que e facil e exato. Uma linha da
    cotacao e uma tabela de preco, entao pode servir a varias linhas da invoice
    (N-para-1 e legitimo aqui).

    Etapa 2 — nome do item, so sobre o residuo. Por construcao, tudo que sobrou
    da Etapa 1 tem preco que nao bate com nenhuma cotacao — ou seja, e
    exatamente onde estao as divergencias. Guloso e 1-para-1.

    Etapa 3 — o que sobrou fica 'unmatched'.

    Por que o preco nao pode ser chave unica do par: se o par so existisse
    quando o preco bate, todo item faturado com preco errado sairia como "sem
    cotacao" e a divergencia que se quer achar ficaria invisivel.

    `sinonimos`: de-para de vocabulario ({token -> termo alvo}, de
    dim_item_sinonimo), carregado uma vez pelo chamador e repassado a todas as
    normalizacoes. `None` -> match sobre texto cru, so mais fraco.

    A ordem de `invoice_lines` e preservada no retorno.
    """
    quotes = [q for q in quote_lines if q.price is not None and Decimal(str(q.price)) > 0]

    matches: dict[Any, ItemMatch] = {}
    residual: list[InvoiceLine] = []

    # --- Etapa 1: assinatura de preco -------------------------------------
    for line in invoice_lines:
        if line.unit_price is None:
            residual.append(line)
            continue

        candidates = [
            q for q in quotes
            if within_tolerance(line.unit_price, q.price, abs_tol, pct_tol)
            and not grades_conflict(line.description, q.item_name)
        ]
        if not candidates:
            residual.append(line)
            continue

        # Preco igual nao basta: dois produtos sem relacao podem custar o mesmo.
        # A descricao precisa dar ao menos um apoio minimo ao par.
        scored = sorted(
            ((float(fuzz.token_set_ratio(norm_item(line.description, sinonimos),
                                         norm_item(q.item_name, sinonimos))), q)
             for q in candidates),
            key=lambda pair: pair[0],
            reverse=True,
        )
        top_score, top_quote = scored[0]
        if top_score < EXACT_MIN_SCORE:
            residual.append(line)
            continue

        empatado = sum(1 for score, _ in scored if score == top_score) > 1
        matches[line.key] = ItemMatch(
            line.key, top_quote.key, "exact", top_score,
            ambiguous=empatado or top_score < CONFIDENT_SCORE,
        )

    # --- Etapa 2: nome do item, sobre o residuo ---------------------------
    if residual and quotes:
        inv_texts = [norm_item(line.description, sinonimos) for line in residual]
        quo_texts = [norm_item(q.item_name, sinonimos) for q in quotes]

        # Laco simples em vez de rapidfuzz.process.cdist: cdist exige numpy so
        # para montar a matriz, e aqui ela e minuscula (dezenas x dezenas, um
        # fornecedor por vez). `score_cutoff` faz o proprio rapidfuzz descartar
        # o que esta abaixo do limiar, devolvendo 0.
        pairs: list[tuple[float, int, int]] = []
        for i, inv in enumerate(inv_texts):
            for j, quo in enumerate(quo_texts):
                score = fuzz.token_set_ratio(inv, quo, score_cutoff=fuzzy_threshold)
                if score and not grades_conflict(residual[i].description,
                                                 quotes[j].item_name):
                    pairs.append((float(score), i, j))
        pairs.sort(key=lambda p: (-p[0], p[1], p[2]))

        used_invoice: set[int] = set()
        used_quote: set[int] = set()
        for score, i, j in pairs:
            if i in used_invoice or j in used_quote:
                continue
            used_invoice.add(i)
            used_quote.add(j)
            matches[residual[i].key] = ItemMatch(
                residual[i].key, quotes[j].key, "fuzzy", score,
                ambiguous=score < CONFIDENT_SCORE,
            )

    # --- Etapa 3: o que sobrou --------------------------------------------
    return [
        matches.get(line.key, ItemMatch(line.key, None, "unmatched", None))
        for line in invoice_lines
    ]


# ---------------------------------------------------------------------------
# Match de itens contra o PO do Catapult (comparacao='erp')
#
# Cascata diferente da de `match_items`: lá o preço decide primeiro porque a
# invoice não carrega código de produto; aqui o PO tem `Supplier Unit ID` /
# scancode, então o código decide primeiro e o preço só entra DEPOIS do match,
# como um dos dois pontos comparados (o outro é quantidade) — nunca para
# achar o par.
# ---------------------------------------------------------------------------

_CODE_RE = re.compile(r"\D+")


def norm_code(value: str | None) -> str:
    """So digitos, sem zero a esquerda — pra comparar item_code/upc da
    invoice com Supplier Unit ID/scancode do PO, que formatam o mesmo codigo
    de jeitos diferentes ('0012345' vs '12345', hifen, espaco).

    Cai pra vazio quando nao sobra digito nenhum (ou o valor so tinha zeros).
    Vazio nunca casa com nada, nem com outro vazio — ver `match_items_po`.
    """
    if not value:
        return ""
    return _CODE_RE.sub("", str(value)).lstrip("0")


class POLine(NamedTuple):
    """Uma linha de item do PO (Purchase Order) do Catapult, como raspada da
    aba Items (`commons/catapult`).

    `receipt_alias` vem na PRÓPRIA linha do PO (coluna "Receipt Alias" da
    grade) — confirmado contra o ambiente real, não precisa de uma ponte
    separada por `dim_item_catapult` pra resolver nome de invoice que não
    bate com `item_name`. `invoiced_total_cost` é o valor TOTAL da linha,
    não preço unitário.

    `ordered`/`received` NÃO estão sempre na mesma unidade um do outro: pra
    item de peso variável (açougue) o Catapult registra `ordered` em caixas
    (o que se pede) e `received` em peso (o que se pesa na doca) — confirmado
    contra o Catapult real (caso Cheney Brothers). Ver
    `commons/matcher.py::qty_matches_po_cases` e
    `conciliacao/reconcile_erp.py::_comparar_grupo`.

    `unit` é a coluna "Unit" da grade Items do PO — "Single Unit" quando
    `ordered`/`invoiced_total_cost` estão em UNIDADES INDIVIDUAIS do item, ou
    "Case"/algo equivalente quando estão em CAIXA/PACOTE (achado do cliente:
    item de mercearia com pack embutido na descrição, ex. "24x200g" — o
    mesmo formato de descrição vale tanto pra PO em "Single Unit", que exige
    multiplicar a quantidade da invoice pelo N do pacote pra bater, quanto
    pra PO em "Case", onde a quantidade IMPRESSA já bate direto e multiplicar
    quebraria a comparação). É o sinal de verdade pra decidir qual dos dois
    -- ver `conciliacao/reconcile_erp.py::_comparar_grupo`. `None` quando a
    coluna não veio raspada (PO antigo, ou célula vazia) — quem usa cai pro
    fallback antigo (categoria do fornecedor).
    """

    key: Any
    supplier_unit_id: str | None
    scancode: str | None
    item_name: str | None
    ordered: Decimal | None = None
    received: Decimal | None = None
    invoiced_total_cost: Decimal | None = None
    receipt_alias: str | None = None
    unit: str | None = None


class InvoiceCodeLine(NamedTuple):
    """Linha da invoice, só o que o match contra o PO precisa pra achar o
    par. Quantidade e preço entram depois, na comparação (fora do motor de
    match — ver `conciliacao/reconcile_erp.py`).

    `handwritten_code` é candidato de código igual a `item_code`/`upc`, não
    um terceiro tipo — só existe porque, pra alguns fornecedores (confirmado
    contra o Catapult real: Cheney Brothers), o código IMPRESSO na nota é do
    catálogo do fornecedor e não bate com o Catapult, mas o número escrito à
    mão na linha é o scancode/Supplier Unit ID de verdade. Ver
    `match_items_po` — só "cola" quando bate com um código real do PO, então
    não arrisca casar errado em fornecedor onde a anotação à mão é outra
    coisa (ex.: código da cotação semanal, sem relação com o Catapult)."""

    key: Any
    description: str | None
    item_code: str | None
    upc: str | None
    handwritten_code: str | None = None


def _po_zerado(po: "POLine") -> bool:
    """True quando a linha do PO tem `received` E `invoiced_total_cost`
    CONHECIDOS e os DOIS zero — sinal de que nada foi de fato recebido nem
    faturado contra essa linha neste PO, mesmo que `ordered` nao seja zero
    (caso real Restaurant Depot/21147023139465080: a linha 'Agua Pure Life'
    — catalogo do item vendido a unidade — tinha ordered=160 mas
    received=0/invoiced_total_cost=$0, enquanto a linha certa do PACK de 40
    unidades, que era o que a invoice de fato vendia, tinha os dois
    preenchidos ($21.80/4 recebidos) e só foi achada por nome depois desta
    ficar de fora. `ordered` sozinho não prova atividade real — é só o que
    foi pedido, não o que chegou/foi cobrado). So conta quando os dois
    campos foram raspados (nenhum None): quando faltam (fixture de teste sem
    esses campos, ou raspagem que nao capturou a celula), a falta de dado
    nao e prova de que a linha e zerada — ver `match_items_po`."""
    recebido, custo = po.received, po.invoiced_total_cost
    return recebido is not None and custo is not None and recebido == 0 and custo == 0


class POMatch(NamedTuple):
    invoice_key: Any
    po_key: Any | None
    match_level: str               # 'codigo' | 'nome' | 'unmatched'
    match_score: float | None
    ambiguous: bool = False        # so 'nome' produz score < CONFIDENT_SCORE


def match_items_po(
    invoice_lines: Sequence[InvoiceCodeLine],
    po_lines: Sequence[POLine],
    fuzzy_threshold: float = FUZZY_THRESHOLD,
    *,
    sinonimos: Mapping[str, str] | None = None,
) -> list[POMatch]:
    """Casa itens da invoice com itens do PO (Catapult), em cascata.

    Etapa 1 — codigo. `item_code`/`upc`/`handwritten_code` da invoice contra
    `supplier_unit_id`/`scancode` do PO, normalizados por `norm_code` — os
    tres sao candidatos de codigo, tentados nessa ordem, e o primeiro que
    bater com o PO ganha. `handwritten_code` entra por ultimo de proposito:
    quando o codigo IMPRESSO bate, ele e mais confiavel; quando nao bate com
    nada (caso Cheney Brothers — o impresso e catalogo do fornecedor, nao do
    Catapult), o numero escrito a mao e a ultima tentativa antes de cair pro
    nome. So "cola" se bater de verdade com um codigo real do PO — em
    fornecedor onde a anotacao a mao e outra coisa (ex.: codigo da cotacao
    semanal), simplesmente nao acha nada em `code_index` e a linha segue pro
    residuo normalmente, sem risco de casar errado. O PO nunca tem linha
    duplicada (garantia de quem popula `po_lines`), entao um codigo
    normalizado aponta pra no maximo um item — ao contrario de `match_items`,
    aqui nao existe N-para-1 legitimo. Nem toda invoice tem algum desses
    codigos — sem nenhum, a linha cai direto no residuo.

    Um candidato de codigo que bate com uma linha de PO "zerada" (`received`
    e `invoiced_total_cost` conhecidos e os dois 0 — `ordered` sozinho não
    conta, ver `_po_zerado`) NAO conta como match — essa linha nunca foi de
    fato recebida/faturada neste PO, so existe na grade porque o Catapult
    lista o catalogo inteiro do fornecedor (ou um cadastro alternativo do
    mesmo item, caso Restaurant Depot abaixo). Aceitar esse "match" rouba a
    linha certa (que só seria achada na Etapa 2, por nome) e deixa
    `valor_po`/`qtd_po` com zero em vez do valor real. Nesse caso a linha da
    invoice cai pro residuo, como se o codigo nao tivesse batido com nada.

    Dois casos reais confirmados: MENA/11126, Sococo Agua de Coco (codigo
    impresso colidiu com o Supplier Unit ID de um item do catalogo sem
    nenhuma atividade — `ordered`/`received`/`invoiced_total_cost` todos 0);
    Restaurant Depot/21147023139465080, Nestle Pure Life 40x0.5L (o item_code
    da invoice bateu com o cadastro do produto vendido A UNIDADE, `ordered`=
    160 mas `received`/`invoiced_total_cost`=0 — o PACK de 40 unidades que a
    invoice de fato vendia estava numa linha de PO SEPARADA, só achada por
    nome depois deste fix).

    Etapa 2 — nome do item, fuzzy (`norm_item` + `token_set_ratio`), sobre o
    residuo da Etapa 1 e so contra item do PO ainda nao reclamado E nao
    "zerado" (mesmo `_po_zerado` da Etapa 1 — uma linha decoy pode ter nome
    textualmente mais parecido com a descricao da invoice do que a linha
    certa, caso real Restaurant Depot acima: 'Agua Pure Life' bate melhor
    por nome com "Nestle Pure Life - Purified Water - 40/0.5L" do que 'Pack
    Agua 40un Nestle Pure Life 500ml', mas é a linha errada — sem esse
    filtro aqui, rejeitar so na Etapa 1 nao bastava, o nome roubava a linha
    certa de novo na Etapa 2). Tenta o melhor score entre `item_name` e
    `receipt_alias` de cada `POLine` — a invoice costuma falar mais perto do
    `receipt_alias` (grafia curta, de recibo) do que do `item_name` de
    cadastro. Guloso e 1-para-1: maior score fica com o par, o resto tenta o
    proximo candidato acima do limiar.

    Etapa 3 — o que sobrou fica 'unmatched'.

    A ordem de `invoice_lines` e preservada no retorno. Pra saber quais itens
    do PO nenhuma linha da invoice reclamou (o "reverse check" do
    fluxograma — recebido/pedido mas nao faturado), ver `po_items_sem_invoice`.
    """
    code_index: dict[str, POLine] = {}
    for po in po_lines:
        for codigo in (norm_code(po.supplier_unit_id), norm_code(po.scancode)):
            if codigo:
                code_index.setdefault(codigo, po)

    matches: dict[Any, POMatch] = {}
    claimed: set[Any] = set()
    residual: list[InvoiceCodeLine] = []

    # --- Etapa 1: codigo ----------------------------------------------
    for line in invoice_lines:
        po = next(
            (code_index[c] for c in (norm_code(line.item_code), norm_code(line.upc),
                                      norm_code(line.handwritten_code))
             if c and c in code_index and not _po_zerado(code_index[c])),
            None,
        )
        if po is None:
            residual.append(line)
            continue
        matches[line.key] = POMatch(line.key, po.key, "codigo", None)
        claimed.add(po.key)

    # --- Etapa 2: nome (item_name OU receipt_alias), sobre o residuo,
    # contra PO ainda nao reclamado --------------------------------------
    disponiveis = [po for po in po_lines if po.key not in claimed and not _po_zerado(po)]
    if residual and disponiveis:
        inv_texts = [norm_item(l.description, sinonimos) for l in residual]

        pairs: list[tuple[float, int, int]] = []
        for i, inv in enumerate(inv_texts):
            for j, po in enumerate(disponiveis):
                score = max(
                    (fuzz.token_set_ratio(inv, norm_item(nome, sinonimos),
                                          score_cutoff=fuzzy_threshold) or 0.0
                     for nome in (po.item_name, po.receipt_alias) if nome),
                    default=0.0,
                )
                if score:
                    pairs.append((float(score), i, j))
        pairs.sort(key=lambda p: (-p[0], p[1], p[2]))

        used_invoice: set[int] = set()
        used_po: set[int] = set()
        for score, i, j in pairs:
            if i in used_invoice or j in used_po:
                continue
            used_invoice.add(i)
            used_po.add(j)
            matches[residual[i].key] = POMatch(
                residual[i].key, disponiveis[j].key, "nome", score,
                ambiguous=score < CONFIDENT_SCORE,
            )
            claimed.add(disponiveis[j].key)

    # --- Etapa 2b: codigo contra linha ZERADA, ultimo recurso ----------------
    # O codigo bate, mas a linha nao foi recebida/faturada neste PO: o item da
    # invoice existe e a divergencia (valor/quantidade contra $0) e o que se
    # quer mostrar — spec-po-zerado-ultimo-recurso.
    for line in invoice_lines:
        if line.key in matches:
            continue
        po = next(
            (code_index[c] for c in (norm_code(line.item_code), norm_code(line.upc),
                                      norm_code(line.handwritten_code))
             if c and c in code_index and code_index[c].key not in claimed),
            None,
        )
        if po is not None:
            matches[line.key] = POMatch(line.key, po.key, "codigo", None)
            claimed.add(po.key)

    # --- Etapa 3: o que sobrou --------------------------------------------
    return [
        matches.get(line.key, POMatch(line.key, None, "unmatched", None))
        for line in invoice_lines
    ]


def po_items_sem_invoice(
    po_lines: Sequence[POLine], matches: Sequence[POMatch],
) -> list[Any]:
    """Chaves do PO que nenhuma linha da invoice reclamou.

    O "reverse check" do fluxograma: item pedido/recebido no PO mas sem par
    em nenhuma linha da invoice processada nesta chamada.

    Hoje isto so alimenta relatorio/log: `fat_conciliacao_item` exige
    `id_invoice_item` (NOT NULL), entao persistir esta lista como linha
    propria ainda depende de estender o schema — fora do escopo desta rodada,
    que e so o motor.
    """
    reclamados = {m.po_key for m in matches if m.po_key is not None}
    return [po.key for po in po_lines if po.key not in reclamados]


def qty_matches_po(
    invoice_qty: Decimal | float | None,
    po_ordered: Decimal | float | None,
    po_received: Decimal | float | None,
    abs_tol: Decimal = ABS_TOL,
    pct_tol: Decimal = PCT_TOL,
) -> bool:
    """True se invoice.qtd, PO.Ordered e PO.Received batem entre si, par a
    par, dentro da tolerancia (`within_tolerance`, igual ao resto do motor).

    Falta de qualquer um dos tres e "nao da pra comparar", nao "bate" — quem
    chama decide o que fazer com o None (ver `conciliacao/reconcile_erp.py`).
    """
    if invoice_qty is None or po_ordered is None or po_received is None:
        return False
    return (within_tolerance(invoice_qty, po_ordered, abs_tol, pct_tol)
            and within_tolerance(invoice_qty, po_received, abs_tol, pct_tol)
            and within_tolerance(po_ordered, po_received, abs_tol, pct_tol))


def qty_matches_po_cases(
    invoice_cases: Decimal | float | None,
    po_ordered: Decimal | float | None,
    abs_tol: Decimal = ABS_TOL,
    pct_tol: Decimal = PCT_TOL,
) -> bool:
    """True se a contagem de CAIXAS da invoice bate com `Ordered` do PO.

    Regra de negocio do acougue (item de peso variavel, confirmada com o
    cliente): no pedido, o que se sabe com exatidao e a caixa — o peso so e
    conhecido na conferencia, porque varia por natureza do produto (fresco x
    congelado, que carrega agua a mais). Por isso a divergencia de
    QUANTIDADE compara caixa contra caixa (`invoice_cases` x `po_ordered`).

    `po_received` fica de fora de proposito: no Catapult, para item catch-
    weight, `Received` e o peso realmente pesado na doca, nao uma contagem
    de caixas — nao e comparavel a `Ordered` nem a `invoice_cases`. O peso
    continua validado do lado do VALOR da linha (`compare_price`), nao aqui.

    Falta de qualquer um dos dois e "nao da pra comparar", nao "bate" —
    mesma convencao de `qty_matches_po`.
    """
    if invoice_cases is None or po_ordered is None:
        return False
    return within_tolerance(invoice_cases, po_ordered, abs_tol, pct_tol)


def invoice_line_total(
    quantity: Decimal | float | None,
    unit_price: Decimal | float | None,
    total_price: Decimal | float | None,
) -> Decimal | None:
    """Valor faturado da linha: `valor_linha`; se ausente, cai pra
    qtd x preco unitario (mesmo fallback do fluxograma)."""
    if total_price is not None:
        return Decimal(str(total_price))
    if quantity is not None and unit_price is not None:
        return Decimal(str(quantity)) * Decimal(str(unit_price))
    return None

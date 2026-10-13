"""Conciliação Invoice × PO (Catapult) — comparação item a item, `comparacao='erp'`.

Implementa a cascata do fluxograma da Etapa 3: casa cada item da invoice com
um item do PO por código primeiro (`item_code`/`upc` x `Supplier Unit ID`/
`scancode`), cai para nome por aproximação no resíduo (contra `item_name` OU
`receipt_alias` do PO — as duas colunas já vêm na própria linha, confirmado
contra o Catapult real), e só então compara quantidade e valor da linha (x
Invoiced Total Cost) do par achado. Quantidade: item comum compara invoice x
Ordered x Received (`qty_matches_po`); item de peso variável — açougue,
`cases` preenchido na leitura — compara CAIXA da invoice x Ordered
(`qty_matches_po_cases`), porque `Received` do Catapult é peso pesado na
doca e não é comparável a `Ordered`/caixa (ver `_comparar_grupo` abaixo e
`commons/vision/schema.py::InvoiceItem.cases`). O motor puro mora em
`commons/matcher.py` (`match_items_po`); aqui é só a regra de uma invoice.

Depois do casamento por código/nome, `_herdar_po_de_variante_de_estado`
resgata a linha que ficou sem PO por ser a mesma carne entregue em estado
diferente (fresco x congelado — o casamento por nome é guloso e 1-para-1,
então só uma das duas reclama o PO). Ela herda o `po_key` da irmã já
casada, pra cair no mesmo grupo e ser somada com ela antes da comparação.

**Não resolve fornecedor nem qual PO pertence a qual invoice.** Mesmo desenho
de `conciliacao/reconcile_quote.py::reconcile_one_invoice`, que já recebe as
`quotes` resolvidas em vez de descobrir sozinho o ciclo — aqui quem chama já
traz `po_lines` prontas (de `commons.catapult.scrape_po_items` +
`to_po_lines`, buscado pelo `Invoice Number` do próprio PO — ver
`commons/catapult/__init__.py`).

Testável sem Postgres — fixtures em memória, como `tests/test_matcher.py` e
`tests/test_resolver_fornecedor.py`.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Sequence

from commons.matcher import (
    ABS_TOL,
    PCT_TOL,
    InvoiceCodeLine,
    POLine,
    POMatch,
    compare_price,
    invoice_line_total,
    match_items_po,
    mesmo_item_estado_diferente,
    norm_supplier,
    norm_text,
    po_items_sem_invoice,
    qty_matches_po,
    qty_matches_po_cases,
)
from commons.logging_config import get_logger
from domain.conciliacao_codes import IssueCode

log = get_logger(__name__)

NO_PO_FOR_ITEM = IssueCode.NO_PO_FOR_ITEM
QTY_MISMATCH_PO = IssueCode.QTY_MISMATCH_PO
PRICE_MISMATCH_PO = IssueCode.PRICE_MISMATCH_PO
HANDWRITTEN_PRESENT = IssueCode.HANDWRITTEN_PRESENT
SKIPPED_INSUMO_ANNOTATION = IssueCode.SKIPPED_INSUMO_ANNOTATION
PO_NAO_ENCONTRADA = IssueCode.PO_NAO_ENCONTRADA

_PALAVRA_INSUMO = "insumo"

# --- Tolerância de preço por fornecedor -------------------------------------
#
# Sobrepõe ABS_TOL/PCT_TOL padrão do motor (commons/matcher.py) pra quem
# pediu regra própria: carne e FreshPoint exigem bater EXATO com o valor da
# invoice (zero tolerância — qualquer diferença é divergência); Triunfo e
# Julina toleram só arredondamento de sub-centavo (até $0.001) — qualquer
# coisa igual ou maior que um centavo já é divergência de verdade pra esses
# dois. Pedido do usuário, sem caso real documentado ainda (ao contrário das
# outras regras deste módulo).
_TOLERANCIA_ZERO = (Decimal("0"), Decimal("0"))
_TOLERANCIA_SUBCENTAVO = (Decimal("0.001"), Decimal("0"))

# Chave = trecho do nome do fornecedor LIDO na invoice (`nome_fornecedor`),
# normalizado (`norm_supplier` — maiúsculas, sem pontuação/sufixo societário)
# — não o nome truncado do Catapult (`dim_fornecedor_alias(origem='erp')`),
# que corta no meio da palavra ('Fresh Poin') e não bate por substring com
# o nome cheio. Comparação por substring de propósito, pra não depender de
# casar a razão social inteira ('Triunfo' cobre 'TRIUNFO FOODS IMPORT
# EXPORT CORP' e qualquer variação de sufixo).
_TOLERANCIA_POR_FORNECEDOR: dict[str, tuple[Decimal, Decimal]] = {
    "TRIUNFO": _TOLERANCIA_SUBCENTAVO,
    "JULINA": _TOLERANCIA_SUBCENTAVO,
    "FRESHPOINT": _TOLERANCIA_ZERO,
}


# --- Tolerancia de 1.00 no valor da linha (spec-tolerancia-preco-1) ----------
#
# Diferenca abaixo disso entre o valor do Catapult e o da invoice: o Invoiced
# Total Cost e editado no Catapult para o valor da invoice (so a edicao usa isto).
# Ficam sem edicao: os produtos abaixo e os fornecedores com tolerancia propria
# (carne, `_TOLERANCIA_POR_FORNECEDOR`).
_TOLERANCIA_VALOR = Decimal("1.00")

# Chave = trecho do nome do fornecedor (`norm_supplier`); valor = produtos
# isentos dele, normalizados por `norm_text`, casados por palavras inteiras.
_ISENTOS_POR_FORNECEDOR: dict[str, tuple[str, ...]] = {
    "PANEBRAS": (
        "PAO", "PAES", "CREME DE LEITE", "LEITE CONDENSADO",
        "BREAD", "BREADS", "HEAVY CREAM", "HEAVY WHIPPING CREAM", "CONDENSED MILK",
    ),
}

# Frios: isento em qualquer fornecedor, por palavra da descricao do produto.
_PALAVRAS_FRIOS: tuple[str, ...] = (
    "PRESUNTO", "QUEIJO", "QUEIJOS", "SALAME", "PEITO DE PERU",
    "MORTADELA", "SALSICHA", "SALSICHAS", "BACON",
    "HAM", "MOZZARELLA", "CHEDDAR", "PARMESAN", "PROVOLONE", "GOUDA",
    "SWISS CHEESE", "SLICED CHEESE", "SHREDDED CHEESE", "SALAMI", "TURKEY BREAST", "BOLOGNA",
    "SAUSAGE", "SAUSAGES", "HOT DOG", "HOT DOGS",
)


def _descricao_tem(descricao: str | None, termos: tuple[str, ...]) -> bool:
    """True se algum termo aparece na descricao como palavras inteiras
    (caixa/acento/pontuacao ignorados): 'PAO' nao casa 'PAOLA'."""
    texto = f" {norm_text(descricao)} "
    return any(f" {norm_text(t)} " in texto for t in termos)


def _grupo_isento(
    grupo: list[dict], supplier_name: str | None, categoria_fornecedor: str | None,
) -> bool:
    """True quando o grupo nao tem o Invoiced Total Cost editado no Catapult (sem a
    tolerancia de 1.00): fornecedor com tolerancia propria, produto isento do
    fornecedor ou Frios. Basta UMA linha do grupo ser isenta."""
    nome_norm = norm_supplier(supplier_name)
    if categoria_fornecedor == "carne" or any(c in nome_norm for c in _TOLERANCIA_POR_FORNECEDOR):
        return True
    produtos = tuple(
        p for chave, ps in _ISENTOS_POR_FORNECEDOR.items() if chave in nome_norm for p in ps
    )
    return any(
        _descricao_tem(i.get("description"), _PALAVRAS_FRIOS + produtos) for i in grupo
    )


def _valor_a_gravar(
    original: Decimal | None, valor_invoice: Decimal | None, descricao: str | None,
) -> Decimal | None:
    """Valor da invoice (2 casas, ROUND_HALF_UP) a gravar na celula Invoiced Total
    Cost do Catapult, ou None quando nao se edita: diferenca de `_TOLERANCIA_VALOR`
    ou mais, valor igual, ou Catapult zerado/vazio (linha ainda nao faturada).
    A tolerancia so decide a EDICAO; a comparacao nao tem tolerancia propria."""
    if original is None or valor_invoice is None or original == 0:
        return None
    if abs(original - valor_invoice) >= _TOLERANCIA_VALOR:
        return None
    novo = valor_invoice.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if novo == original:
        return None
    return novo


def _resolver_tolerancia_preco(
    supplier_name: str | None, categoria_fornecedor: str | None,
) -> tuple[Decimal, Decimal]:
    """Decide `(abs_tol, pct_tol)` pra `compare_price` desta invoice.

    `categoria_fornecedor` vem de `dim_fornecedor.categoria`
    (`domain.service.conciliacao_service.fetch_supplier_aliases`, resolvido
    por `crawler/flow/reconcile_erp_flow.py` antes de chamar
    `reconcile_items_against_po`) — carne tem regra própria independente de
    QUAL fornecedor de carne é. Fora carne, casa por nome contra
    `_TOLERANCIA_POR_FORNECEDOR`; sem nenhuma regra específica (fornecedor
    não mapeado ou sem regra), cai na tolerância padrão do motor."""
    if categoria_fornecedor == "carne":
        return _TOLERANCIA_ZERO
    nome_norm = norm_supplier(supplier_name)
    for chave, tolerancia in _TOLERANCIA_POR_FORNECEDOR.items():
        if chave in nome_norm:
            return tolerancia
    return (ABS_TOL, PCT_TOL)


def tem_anotacao_insumo(items: list[dict], general_handwritten_notes: str | None = None) -> bool:
    """True se alguma linha da nota tem 'insumo' na anotação à mão
    (`handwritten_notes`) OU se há uma anotação solta na página (não presa a
    nenhum item — `general_handwritten_notes`, ver `commons/vision/schema.py`)
    com a palavra — regra de negócio: nota inteira não vai para o Catapult
    nesse caso (ver `crawler/flow/reconcile_erp_flow.py::_conciliar_invoice`,
    que checa isto ANTES de `_buscar_po`).

    A anotação solta é o caso comum na prática — 'insumo' costuma ser escrito
    embaixo da tabela de itens, sobre a nota inteira, não ao lado de uma
    linha específica (caso real: Restaurant Depot/Beachline West, nota
    21146543173478892 — 'Insumo Confeitaria' escrito na margem das duas
    páginas, sem estar preso a nenhum item)."""
    if _PALAVRA_INSUMO in (general_handwritten_notes or "").lower():
        return True
    return any(
        _PALAVRA_INSUMO in (item.get("handwritten_notes") or "").lower()
        for item in items
    )


# Desempate por valor quando mais de um candidato casa a MESMA fração de
# itens (abaixo). Acima desta divergência relativa, o valor do lado PO não é
# confiável o bastante pra desempatar — melhor devolver `None` (a nota cai
# pra "sem PO", revisão manual) do que arriscar escolher a PO errada.
# Achado real (MENA/11131, 2026-09): item recorrente do fornecedor
# ("Massa Pastel Recortada", código 00130) aparecia com atividade real em 3
# PO's históricas diferentes — escolher pela ORDEM de listagem (o
# comportamento antigo) pegou uma cujo `Invoiced Total Cost` ($159.90) não
# tinha nada a ver com o valor faturado ($239.85); as outras duas batiam
# ainda pior (30%+ de diferença). 10% cobre arredondamento/desconto
# plausível sem abrir margem pra coincidência de código entre pedidos
# completamente diferentes.
_DESEMPATE_DIVERGENCIA_MAX = Decimal("0.10")


def escolher_po_por_itens(
    items: list[dict],
    candidatos: Sequence[Sequence[POLine]],
    sinonimos: dict | None = None,
) -> int | None:
    """Entre vários PO 'Ordered' (ou históricos) do mesmo fornecedor (busca
    por nome não isola mais um único PO como a busca por invoice fazia),
    escolhe o ÍNDICE do candidato cujos itens melhor casam com os desta
    invoice.

    Reaproveita `match_items_po` (mesmo motor de `reconcile_items_against_po`)
    contra cada candidato e mede a fração de itens da invoice que casaram por
    código ou nome — o candidato com a maior fração vence, SE for único.
    Quando mais de um candidato empata na mesma fração (item recorrente do
    fornecedor, mesmo código em pedidos diferentes — ver
    `_DESEMPATE_DIVERGENCIA_MAX`), desempata pelo valor: soma o faturado dos
    itens casados contra o `Invoiced Total Cost` das linhas do PO que eles
    casaram, e fica com o candidato mais próximo — nunca com o primeiro por
    ordem de listagem, que é arbitrário.

    `None` quando NENHUM candidato casou nada (nenhum item bateu por código
    nem por nome em nenhum PO), OU quando o empate não se resolve com
    confiança (nenhum candidato empatado tem valor comparável dentro da
    tolerância) — situação ambígua, quem chama decide o que fazer (hoje:
    `crawler/flow/reconcile_erp_flow.py::_buscar_po` trata como sem PO)."""
    invoice_refs = [
        InvoiceCodeLine(i["id"], i.get("description"), i.get("item_code"), i.get("upc"),
                        i.get("handwritten_code"))
        for i in items
    ]
    if not invoice_refs:
        return None

    scores = [_fracao_casada(invoice_refs, po_lines, sinonimos) for po_lines in candidatos]
    melhor_score = max(scores, default=0.0)
    if melhor_score == 0.0:
        return None

    empatados = [i for i, s in enumerate(scores) if s == melhor_score]
    if len(empatados) == 1:
        return empatados[0]

    return _desempatar_por_valor(items, invoice_refs, candidatos, empatados, sinonimos)


def _fracao_casada(
    invoice_refs: list[InvoiceCodeLine],
    po_lines: Sequence[POLine],
    sinonimos: dict | None,
) -> float:
    matches = match_items_po(invoice_refs, po_lines, sinonimos=sinonimos)
    casados = sum(1 for m in matches if m.match_level in ("codigo", "nome"))
    return casados / len(invoice_refs)


def _desempatar_por_valor(
    items: list[dict],
    invoice_refs: list[InvoiceCodeLine],
    candidatos: Sequence[Sequence[POLine]],
    empatados: list[int],
    sinonimos: dict | None,
) -> int | None:
    """Entre os índices empatados, o que tem a menor divergência de valor
    (faturado x `Invoiced Total Cost`), desde que dentro de
    `_DESEMPATE_DIVERGENCIA_MAX`. `None` se nenhum estiver dentro dela."""
    melhor_idx: int | None = None
    melhor_dif: Decimal | None = None
    for i in empatados:
        dif = _dif_valor_candidato(items, invoice_refs, candidatos[i], sinonimos)
        if dif is None or dif > _DESEMPATE_DIVERGENCIA_MAX:
            continue
        if melhor_dif is None or dif < melhor_dif:
            melhor_dif, melhor_idx = dif, i
    return melhor_idx


def _dif_valor_candidato(
    items: list[dict],
    invoice_refs: list[InvoiceCodeLine],
    po_lines: Sequence[POLine],
    sinonimos: dict | None,
) -> Decimal | None:
    """Diferença relativa entre o valor faturado dos itens que casaram (por
    código/nome) neste candidato e o `Invoiced Total Cost` das linhas do PO
    casadas. `None` quando não dá pra comparar — nenhum item casou, ou falta
    valor de algum lado (nunca inventa um número pra desempatar)."""
    matches = match_items_po(invoice_refs, po_lines, sinonimos=sinonimos)
    po_by_key = {po.key: po for po in po_lines}
    items_by_id = {item["id"]: item for item in items}

    valor_invoice = Decimal("0")
    po_keys_casados: set[Any] = set()
    for m in matches:
        if m.match_level not in ("codigo", "nome") or m.po_key is None:
            continue
        item = items_by_id[m.invoice_key]
        valor_linha = invoice_line_total(
            item.get("quantity"), item.get("unit_price"), item.get("total_price"))
        if valor_linha is None:
            return None
        valor_invoice += valor_linha
        po_keys_casados.add(m.po_key)

    if not po_keys_casados:
        return None

    valor_po = Decimal("0")
    for chave in po_keys_casados:
        custo = po_by_key[chave].invoiced_total_cost
        if custo is None:
            return None
        valor_po += custo

    if valor_po == 0:
        return None if valor_invoice != 0 else Decimal("0")
    return abs(valor_invoice - valor_po) / valor_po


def reconcile_items_against_po(
    items: list[dict],
    po_lines: Sequence[POLine],
    sinonimos: dict | None = None,
    supplier_name: str | None = None,
    categoria_fornecedor: str | None = None,
    aplicar_ajustes: bool = True,
) -> dict:
    """Concilia os itens de UMA invoice contra os itens do PO do Catapult.

    `aplicar_ajustes=False`: nao ha ajuste de Invoiced Total Cost (a edicao no Catapult
    nao aconteceu); compara com o valor original do PO.

    `items`: mesmo formato de
    `domain.service.conciliacao_service.fetch_invoice_items_by_headers()` —
    usa `id`, `description`, `quantity`, `unit_price`, `total_price`,
    `item_code`, `upc`, `handwritten_notes`.

    `supplier_name`/`categoria_fornecedor`: usados só pra resolver a
    tolerância de preço desta invoice (ver `_resolver_tolerancia_preco`) —
    sem eles, cai na tolerância padrão do motor pra todo mundo.

    `po_lines` vazio é a NOTA sem PO pra comparar (a busca no Catapult não
    devolveu PO nenhum, foi ambígua demais pra desempatar, ou nenhum
    candidato casou pelos itens — `crawler/flow/reconcile_erp_flow.py::
    _buscar_po`) — problema da invoice, não de item nenhum. Nesse caso só o
    HEADER e cada item ganham `PO_NAO_ENCONTRADA` (veredito
    SEM_REFERENCIA_ITEM, nunca CONFERIDO); os itens não carregam
    `NO_PO_FOR_ITEM` individualmente. Distinto de um item que não bate com NENHUM item
    de uma PO que EXISTE (`po_lines` não vazio, mas este item específico não
    achou par) — aí sim é um problema do item, e `NO_PO_FOR_ITEM` continua
    marcado nele.

    Quando mais de uma linha da invoice casa com o MESMO item do PO — caso
    real: Restaurant Depot escaneia o mesmo produto em caixas separadas no
    caixa, a invoice sai com 2 linhas do mesmo UPC — a comparação de
    quantidade e valor usa a SOMA das linhas do grupo contra o PO, não linha
    a linha. Comparar cada linha isolada contra o total inteiro do PO
    divergiria sempre, por construção, mesmo quando a soma bate certinho.

    Retorna `{"items": [...], "issue_codes": [...], "has_issue": bool,
    "needs_review": bool, "po_orphans": [...], "ajustes_po": [...]}`.
    `ajustes_po`: celulas Invoiced Total Cost a gravar no Catapult
    (spec-catapult-editar-invoiced-cost). `items` está pronto pra
    `conciliacao_service.save_reconciliation_items()` (mesmas chaves que
    `reconcile_one_invoice` produz para a cotação: `id_invoice_item`,
    `issue_codes`, `qty_invoice`, `price_invoice`, `price_diff`,
    `price_diff_pct`, ...). `po_orphans` são as chaves de `po_lines` que
    nenhum item da invoice reclamou — pedido/recebido mas não faturado nesta
    nota; ainda sem onde persistir (ver docstring de `po_items_sem_invoice`).
    """
    invoice_refs = [
        InvoiceCodeLine(i["id"], i.get("description"), i.get("item_code"), i.get("upc"),
                        i.get("handwritten_code"))
        for i in items
    ]
    matches = match_items_po(invoice_refs, po_lines, sinonimos=sinonimos)
    matches = _herdar_po_de_variante_de_estado(items, matches, sinonimos)
    matches_by_item = {m.invoice_key: m for m in matches}
    po_by_key = {po.key: po for po in po_lines}

    abs_tol, pct_tol = _resolver_tolerancia_preco(supplier_name, categoria_fornecedor)
    is_carne = categoria_fornecedor == "carne"

    grupos: dict[Any, list[dict]] = {}
    for item in items:
        po_key = matches_by_item[item["id"]].po_key
        if po_key is not None:
            grupos.setdefault(po_key, []).append(item)

    veredito_por_po_key = {
        po_key: _comparar_grupo(
            grupo, po_by_key[po_key], abs_tol, pct_tol, is_carne,
            _grupo_isento(grupo, supplier_name, categoria_fornecedor) or not aplicar_ajustes,
        )
        for po_key, grupo in grupos.items()
    }

    # Nota inteira sem PO pra comparar — ver docstring acima. Distinto de um
    # item específico sem par numa PO que existe.
    sem_po_na_nota = not po_lines

    linhas: list[dict] = []
    for item in items:
        match = matches_by_item[item["id"]]
        po = po_by_key.get(match.po_key) if match.po_key is not None else None

        row = {
            "id_invoice_item": item["id"],
            "item_order": item.get("item_order"),
            "description_invoice": item.get("description"),
            "qty_invoice": item.get("quantity"),
            "cases_invoice": item.get("cases"),
            "price_invoice": item.get("unit_price"),
            "match_level": match.match_level,
            "match_score": match.match_score,
        }

        issues: list[str] = []
        needs_review = bool(match.ambiguous)
        if item.get("handwritten_notes"):
            issues.append(HANDWRITTEN_PRESENT)
            needs_review = True

        if po is None:
            if sem_po_na_nota:
                # Problema da nota, não deste item — não marca NO_PO_FOR_ITEM
                # (ver docstring). Mas o item leva PO_NAO_ENCONTRADA: sem ele
                # o veredito cairia em CONFERIDO e a linha apareceria como
                # conciliada no painel.
                issues.append(PO_NAO_ENCONTRADA)
                row.update({"issue_codes": issues, "has_issue": True,
                            "needs_review": needs_review})
            else:
                issues.append(NO_PO_FOR_ITEM)
                row.update({"issue_codes": issues, "has_issue": True,
                            "needs_review": needs_review})
            linhas.append(row)
            continue

        veredito = veredito_por_po_key[match.po_key]
        # qty_invoice segue a MESMA regra usada na comparacao (`_comparar_grupo`,
        # a partir de `po.unit`/`_po_unidade_individual`): caixa quando a PO esta
        # em "Case", quantidade/peso quando "Single Unit" (ou fallback is_carne).
        # Sem isto, `qty_invoice` (persistido em fat_conciliacao_item.qtd e
        # exibido no Grafana/relatorio de divergencia) ficava sempre em
        # `quantity`, numa unidade diferente de `qty_po`/`qty_diff` sempre que a
        # comparacao usava caixa — mesma linha, duas unidades.
        if veredito["usar_caixa"] and item.get("cases") is not None:
            row["qty_invoice"] = item.get("cases")
        row.update({
            "id_po_item": po.key,
            "item_name_po": po.item_name,
            "price_other": veredito["total_po"],
            "price_diff": veredito["price_diff"],
            "price_diff_pct": veredito["price_diff_pct"],
            "qty_po": po.ordered,
            "qty_po_received": po.received,
            "qty_diff": veredito["qty_diff"],
            "total_invoice": veredito["total_invoice"],
            "total_po": veredito["total_po"],
        })

        if not veredito["qty_ok"]:
            issues.append(QTY_MISMATCH_PO)
        if not veredito["price_ok"]:
            issues.append(PRICE_MISMATCH_PO)

        divergente = QTY_MISMATCH_PO in issues or PRICE_MISMATCH_PO in issues
        row.update({
            "issue_codes": issues,
            "has_issue": divergente or needs_review,
            "needs_review": needs_review,
        })
        linhas.append(row)

    header_issues = {c for r in linhas for c in r["issue_codes"]}
    if sem_po_na_nota:
        header_issues.add(PO_NAO_ENCONTRADA)
    return {
        "items": linhas,
        "issue_codes": sorted(header_issues),
        "has_issue": any(r["has_issue"] for r in linhas) or sem_po_na_nota,
        "needs_review": any(r["needs_review"] for r in linhas),
        "po_orphans": po_items_sem_invoice(po_lines, matches),
        "ajustes_po": [
            {"po_key": po_key, "valor_catapult": po_by_key[po_key].invoiced_total_cost,
             "valor_invoice": v["ajuste"],
             "supplier_unit_id": po_by_key[po_key].supplier_unit_id,
             "scancode": po_by_key[po_key].scancode,
             "receipt_alias": po_by_key[po_key].receipt_alias,
             "item_name": po_by_key[po_key].item_name}
            for po_key, v in veredito_por_po_key.items() if v["ajuste"] is not None
        ],
    }


def _herdar_po_de_variante_de_estado(
    items: list[dict], matches: list[POMatch], sinonimos: dict | None,
) -> list[POMatch]:
    """Linha que ficou 'unmatched' mas é a MESMA carne que uma linha vizinha
    já casada, só entregue em estado diferente (fresco x congelado) — caso
    real Prime Meats/1175605 (achado do cliente, Leandro Rocha): "CHKN
    BREAST BL/SL DRY GEN FZN" e "CHKN BREAST BL/SL UNSIZED DRY GEN FRSH CVP"
    são o mesmo peito de frango pra quem recebe — o Catapult só tem UMA
    linha de PO pra ele, e o casamento por nome (`match_items_po`, guloso e
    1-para-1) só deixa uma das duas reclamar aquele PO; a outra sobra
    'unmatched' mesmo com tudo certo.

    Aqui ela herda o MESMO `po_key` da irmã que já casou — depois disso os
    dois caem no mesmo grupo em `reconcile_items_against_po` e
    `_comparar_grupo` soma as duas antes de comparar com o PO (o motor já
    sabe fazer isso, só precisava as duas apontarem pro mesmo item).

    Só herda de quem JÁ tem `po_key` de verdade (código ou nome contra o
    PO) — nunca funde duas linhas 'unmatched' entre si sem nenhuma delas
    ter confirmado um PO real."""
    descricao_por_chave = {item["id"]: item.get("description") for item in items}
    ja_casados = [m for m in matches if m.po_key is not None]

    resultado: list[POMatch] = []
    for m in matches:
        if m.po_key is not None:
            resultado.append(m)
            continue
        desc = descricao_por_chave.get(m.invoice_key)
        irma = next(
            (v for v in ja_casados
             if mesmo_item_estado_diferente(desc, descricao_por_chave.get(v.invoice_key), sinonimos)),
            None,
        )
        if irma is None:
            resultado.append(m)
            continue
        resultado.append(POMatch(m.invoice_key, irma.po_key, "estado", None))
    return resultado


def _po_unidade_individual(unit: str | None) -> bool | None:
    """Lê `POLine.unit` (coluna "Unit" da grade Items do Catapult — achado do
    cliente): `True` quando "Single Unit" — `Ordered`/`Invoiced Total Cost`
    estão em UNIDADES INDIVIDUAIS do item, o que exige comparar contra
    `quantity` já multiplicado pelo pack embutido na descrição (regra 8 do
    prompt em `commons/vision/__init__.py`). `False` quando "Case" (ou
    equivalente) — `Ordered` está em CAIXA/PACOTE, a contagem IMPRESSA sem
    multiplicar (`cases`). `None` quando a coluna não veio raspada (PO
    antigo, célula vazia, ou texto que não reconhece) — quem chama cai pro
    fallback por categoria de fornecedor (`is_carne`).

    Achado real (cliente): a MESMA forma de descrição ("<N>x<peso>") aparece
    em PO com os dois `unit` — ex. lasanha em "Single Unit" (`Ordered`=20,
    bate só com `quantity` multiplicado) e manteiga em "Case" (`Ordered`=2,
    bate com a quantidade IMPRESSA — multiplicar quebraria). Sem ver a PO
    não dá pra saber qual é; por isso a Vision não decide mais essa parte —
    só fornece os dois números (`quantity` multiplicado e `cases` original)
    e esta função escolhe qual usar."""
    if not unit:
        return None
    normalizado = unit.strip().lower()
    if "single" in normalizado:
        return True
    if "case" in normalizado:
        return False
    return None


def _comparar_grupo(
    grupo: list[dict], po: POLine, abs_tol: Decimal, pct_tol: Decimal, is_carne: bool,
    isento: bool,
) -> dict:
    """Compara a SOMA de um grupo de linhas da invoice (todas casadas com o
    mesmo item do PO) contra `po.ordered`/`received`/`invoiced_total_cost`.
    Com 1 item só no grupo, é a mesma coisa de comparar a linha sozinha.

    `abs_tol`/`pct_tol`: tolerância de preço já resolvida por fornecedor
    (`_resolver_tolerancia_preco`) — passada pra `compare_price` no lugar do
    padrão do motor.

    A escolha entre comparar `cases` (caixa) ou `quantity` (unidade/peso)
    contra `Ordered` vem, em ordem de preferência, de `po.unit`
    (`_po_unidade_individual` — sinal de verdade, direto da PO) e só cai
    pro `is_carne` (`categoria_fornecedor == "carne"`, resolvido por quem
    chama) quando `po.unit` não veio raspado. Açougue (item de peso
    variável, `cases` preenchido pela leitura): quantidade compara CAIXA da
    invoice x `Ordered` (`qty_matches_po_cases`), não peso — peso só é
    sabido na conferência (varia por natureza do produto) e por isso valida
    o VALOR da linha, não a quantidade (`quantity` continua sendo o peso pra
    fechar `quantity x unit_price`). Fora do açougue, `cases` também pode
    vir preenchido pelo multiplicador de pack da Vision (regra 8 do prompt);
    nesse caso só compara `cases` contra `Ordered` quando a PO de fato está
    em "Case" — comparar `quantity` (já multiplicado) contra uma PO em
    "Case" daria uma divergência falsa (caixa x unidade), o mesmo problema
    ao contrário do que essa função corrige.

    O VALOR (`invoiced_total_cost`) NÃO segue essa mesma troca — confirmado
    contra o Catapult real (achado do cliente): mesmo numa PO em "Single
    Unit", `Invoiced Total Cost` é o total REAL faturado na linha (o mesmo
    que a Vision extrai em `total_price`, nunca recalculado pelo
    multiplicador de pack), porque é dinheiro — não muda com a unidade em
    que a quantidade é contada. Só a comparação de QUANTIDADE depende de
    `po.unit`; o valor usa sempre `invoice_line_total`, como sempre foi."""
    single_unit = _po_unidade_individual(po.unit)
    usar_caixa = (not single_unit) if single_unit is not None else is_carne
    qtd_total = _soma_ou_none(i.get("quantity") for i in grupo)
    cases_total = _soma_ou_none(i.get("cases") for i in grupo)
    valor_total = _soma_ou_none(
        invoice_line_total(i.get("quantity"), i.get("unit_price"), i.get("total_price"))
        for i in grupo
    )
    # Celula a gravar no Catapult (spec-catapult-editar-invoiced-cost). Depois da edicao
    # o Catapult passa a ter o valor da invoice, que e o que a comparacao enxerga.
    ajuste = None if isento else _valor_a_gravar(po.invoiced_total_cost, valor_total, po.item_name)
    total_po = po.invoiced_total_cost if ajuste is None else ajuste
    verdict = compare_price(valor_total, total_po, abs_tol=abs_tol, pct_tol=pct_tol)
    if usar_caixa and cases_total is not None:
        qty_ok = qty_matches_po_cases(cases_total, po.ordered)
        qtd_comparada = cases_total
    else:
        qty_ok = qty_matches_po(qtd_total, po.ordered, po.received)
        qtd_comparada = qtd_total
    # Contra `Ordered` nas duas pontas: é o único número do PO que está na
    # mesma unidade do que se compara (caixa no açougue, quantidade no resto).
    qty_diff = (qtd_comparada - po.ordered) \
        if (qtd_comparada is not None and po.ordered is not None) else None
    return {
        "qty_ok": qty_ok,
        "price_ok": verdict.status == "ok",
        "price_diff": verdict.diff,
        "price_diff_pct": verdict.diff_pct,
        "qty_diff": qty_diff,
        "total_invoice": valor_total,
        "total_po": total_po,
        "ajuste": ajuste,
        "usar_caixa": usar_caixa and cases_total is not None,
    }


def _soma_ou_none(valores) -> Decimal | None:
    """Soma um iterável de `Decimal | None`. `None` se QUALQUER valor faltar
    — não dá pra somar parcial e fingir que o total está completo."""
    total: Decimal | None = None
    for v in valores:
        if v is None:
            return None
        total = v if total is None else total + v
    return total

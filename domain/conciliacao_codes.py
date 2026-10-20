"""Códigos de divergência da conciliação — a fonte da verdade, em código.

Mesmo arranjo de `categorias.py` e `status_exec.py`: o banco guarda a lista de
strings (`fat_conciliacao.issue_codes`, `fat_conciliacao_item.issue_codes`), o
significado mora aqui.

POR QUE ESTE MÓDULO EXISTE

As constantes viviam soltas no topo de `conciliacao/reconcile_quote.py`, e
`conciliacao/conciliacao_db.py` comparava as mesmas strings escritas à mão
(`"no_quote_for_item"`, `"handwritten_present"`, ...). Duas listas do mesmo
vocabulário, sem nada garantindo que combinam.

`issue_codes` é a lista **crua** do que a linha (ou a nota) acumulou:
`['handwritten_present', 'price_above_quote']` é uma divergência de preço numa
nota que também tem anotação à mão. O `cod_status` continua sendo o **veredito
único** derivado dessa lista (`_status_conciliacao` em `conciliacao_service.py`); os dois
convivem — a lista para o painel destrinchar o motivo, o código para agrupar.

`IssueCode` é `StrEnum`: cada membro **é** a sua string, então as comparações com
literal que já existem (`"unit_mismatch" in codes`) seguem funcionando.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "IssueCode",
    "MARCAS_REVISAO",
    "VEREDITO_PRECO",
    "VEREDITO_ERP",
]


class IssueCode(StrEnum):
    """Um código de divergência acumulado por uma linha ou nota conciliada.

    Gravado em `fat_conciliacao(_item).issue_codes` (array de texto). Não há
    CHECK no banco — array não aceita —, então este enum é o único guarda.
    """

    # --- fornecedor (nível da nota)
    SUPPLIER_UNMAPPED = "supplier_unmapped"        # nome lido não casou com alias
    SUPPLIER_FUZZY_MATCH = "supplier_fuzzy_match"  # casou por aproximação, conferir
    SUPPLIER_ERP_ONLY = "supplier_erp_only"        # não-carne: comparação é a do ERP
    NO_QUOTE_FOR_SUPPLIER = "no_quote_for_supplier"  # sem cotação no ciclo

    # --- exclusão (nível da nota) — não é divergência, é decisão de não comparar
    SKIPPED_INSUMO_ANNOTATION = "skipped_insumo_annotation"  # anotação à mão com "insumo"
    INSUMO_INCERTO = "insumo_incerto"  # anotação à mão que pode ser insumo, sem certeza

    # --- busca no Catapult (nível da nota) — a busca por fornecedor não achou nada
    PO_NAO_ENCONTRADA = "po_nao_encontrada"  # busca por fornecedor não devolveu PO nenhum

    # --- preço (nível da linha) — estes viram veredito
    NO_QUOTE_FOR_ITEM = "no_quote_for_item"        # item da nota sem par na cotação
    UNIT_MISMATCH = "unit_mismatch"                # preço por caixa x por libra
    PRICE_ABOVE_QUOTE = "price_above_quote"        # faturado acima do cotado
    PRICE_BELOW_QUOTE = "price_below_quote"        # faturado abaixo do cotado

    # --- comparação contra o PO do Catapult (nível da linha) — comparacao='erp'
    NO_PO_FOR_ITEM = "no_po_for_item"              # item da nota sem par no PO
    QTY_MISMATCH_PO = "qty_mismatch_po"            # invoice x Ordered x Received divergem
    PRICE_MISMATCH_PO = "price_mismatch_po"        # valor da linha x Invoiced Total Cost diverge
    PO_FECHADO_MANUAL = "po_fechado_manual"        # PO fechado a mao no Catapult; item identificado

    # --- qualidade da leitura (nível da linha) — ligam `revisar`, não o veredito
    LOW_VISION_CONFIDENCE = "low_vision_confidence"  # confiança da leitura abaixo do piso
    HANDWRITTEN_PRESENT = "handwritten_present"      # há anotação à mão na linha

    @property
    def descricao(self) -> str:
        return _DESCRICAO[self]


_DESCRICAO: dict[IssueCode, str] = {
    IssueCode.SUPPLIER_UNMAPPED: "Nome do fornecedor lido não casou com nenhum alias.",
    IssueCode.SUPPLIER_FUZZY_MATCH: "Fornecedor casou por aproximação — conferir.",
    IssueCode.SUPPLIER_ERP_ONLY: "Fornecedor não-carne: a comparação dele é contra o ERP.",
    IssueCode.NO_QUOTE_FOR_SUPPLIER: "Fornecedor sem cotação no ciclo da nota.",
    IssueCode.SKIPPED_INSUMO_ANNOTATION: "Nota com anotação à mão de insumo — não enviada ao Catapult.",
    IssueCode.INSUMO_INCERTO: "Anotação à mão pode ser insumo, sem certeza — revisar a invoice.",
    IssueCode.PO_NAO_ENCONTRADA: "Busca por fornecedor no Catapult não devolveu PO nenhum.",
    IssueCode.NO_QUOTE_FOR_ITEM: "Item da nota sem par no lado cotado.",
    IssueCode.UNIT_MISMATCH: "Preço por caixa não compara com preço por libra.",
    IssueCode.PRICE_ABOVE_QUOTE: "Faturado acima do cotado, além da tolerância.",
    IssueCode.PRICE_BELOW_QUOTE: "Faturado abaixo do cotado, além da tolerância.",
    IssueCode.NO_PO_FOR_ITEM: "Item da nota sem par no PO do Catapult.",
    IssueCode.QTY_MISMATCH_PO: "Quantidade faturada não bate com Ordered/Received do PO.",
    IssueCode.PRICE_MISMATCH_PO: "Valor da linha não bate com o Invoiced Total Cost do PO.",
    IssueCode.PO_FECHADO_MANUAL: "PO já fechado à mão no Catapult: item identificado, sem PO para comparar.",
    IssueCode.LOW_VISION_CONFIDENCE: "Leitura do PDF abaixo do piso de confiança.",
    IssueCode.HANDWRITTEN_PRESENT: "Linha com anotação à mão na nota.",
}


# Sinalizações de qualidade da leitura: não são veredito, mas ligam `revisar`.
# Espelha o que estava hardcoded em conciliacao_db.py::_MARCAS_REVISAO.
MARCAS_REVISAO: frozenset[str] = frozenset({
    IssueCode.HANDWRITTEN_PRESENT,
    IssueCode.LOW_VISION_CONFIDENCE,
})

# Códigos que produzem um veredito de preço (ordem de precedência fica no
# _status_conciliacao de conciliacao_service.py). Aqui só para documentar o grupo.
VEREDITO_PRECO: frozenset[str] = frozenset({
    IssueCode.NO_QUOTE_FOR_ITEM,
    IssueCode.UNIT_MISMATCH,
    IssueCode.PRICE_ABOVE_QUOTE,
    IssueCode.PRICE_BELOW_QUOTE,
})

# Mesmo papel de VEREDITO_PRECO, para a comparação contra o PO (comparacao='erp').
VEREDITO_ERP: frozenset[str] = frozenset({
    IssueCode.NO_PO_FOR_ITEM,
    IssueCode.QTY_MISMATCH_PO,
    IssueCode.PRICE_MISMATCH_PO,
})

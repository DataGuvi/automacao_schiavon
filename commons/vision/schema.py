"""Schema da invoice — o que o Claude Vision extrai do PDF.

As descrições dos campos não são documentação: vão no prompt de extração
estruturada, então mudá-las muda o que o modelo devolve.

Vivia em `domain/model/invoice.py`, e `commons/vision` o importava de lá —
uma seta proibida (`commons` não importa `domain`), tolerada num comentário
que argumentava ser "schema puro". Não era: o `model_validator` abaixo é a
rede de segurança da regra 8 do prompt que mora em `commons/vision/__init__.py`.
Ou seja, isto não é um Model de domínio que a Vision empresta — é o
CONTRATO DE SAÍDA da Vision, que só faz sentido ao lado do prompt que ele
espelha. Movido para cá, a seta aponta para a direção permitida: quem precisa
do schema (`domain/service/invoice_service.py`) importa de `commons`, que é
o que a governança já autoriza.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# Achado do cliente: pedir pro modelo aplicar o multiplicador de pack embutido
# na descricao ("<N>X<size>", ex. "12x200g", "90x30g") de forma confiavel em
# TODA linha elegivel de uma nota com varias dezenas de itens nao funciona —
# o LLM aplica em algumas e esquece outras na mesma chamada (confirmado
# reprocessando a mesma nota mais de uma vez com o mesmo prompt: taxa de
# acerto inconsistente). Como a esta altura a escolha entre `quantity`
# (multiplicado) e `cases` (impresso) ja NAO depende de o LLM "decidir" nada —
# e so um recorte mecanico do texto, decidido depois na conciliacao contra
# `POLine.unit` — dá pra tirar essa etapa da mao do modelo e fazer aqui,
# deterministicamente, sempre que ele deixar passar.
_PACK_SIZE_RE = re.compile(
    r"(\d+)\s*x\s*\d+(?:[.,]\d+)?\s*(?:kg|gr|ml|lb|oz|g|l)\b", re.IGNORECASE
)

# Pack impresso em coluna propria, com barra: "12/12 oz", "6/2LB", "6/12 oz"
# (spec-pack-size-coluna). So o N conta; a unidade do size nao e convertida.
_PACK_COLUNA_RE = re.compile(
    r"^\s*(\d+)\s*/\s*\d+(?:[.,]\d+)?\s*(?:kg|gr|ml|lbs|lb|oz|g|l)\b", re.IGNORECASE
)


def _n_do_pack(pack_size: str | None, description: str | None) -> int:
    """N do pack ('12/12 oz' -> 12; descricao '12x500 GR' -> 12), ou 0 sem padrao."""
    m = _PACK_COLUNA_RE.match(pack_size or "") or _PACK_SIZE_RE.search(description or "")
    return int(m.group(1)) if m else 0

# Data impressa: tres numeros separados por '/', '-' ou '.'. A Vision converte
# a data para ISO "de cabeca" e ja trocou dia/mes/ano em nota americana
# (mes/dia/ano) — a nota saiu da janela da conciliacao e ficou presa.
# A conversao sai da mao do modelo e e feita aqui (spec-data-invoice-mdy).
_DATA_IMPRESSA_RE = re.compile(r"^\s*(\d{1,4})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{2,4})\s*$")


def _converter_data_impressa(impressa: str | None) -> str | None:
    """Data impressa -> ISO, ou None quando nao da para converter com certeza.

    `AAAA/MM/DD` e ano/mes/dia. Os demais sao mes/dia/ano (padrao americano),
    salvo quando o primeiro numero passa de 12 — ai so pode ser dia/mes/ano.
    Ano com 2 digitos vira 20AA.
    """
    m = _DATA_IMPRESSA_RE.match(impressa or "")
    if not m:
        return None
    a, b, c = m.groups()
    if len(a) == 4:
        ano, mes, dia = int(a), int(b), int(c)
    elif int(a) > 12:
        dia, mes, ano = int(a), int(b), int(c)
    else:
        mes, dia, ano = int(a), int(b), int(c)
    if ano < 100:
        ano += 2000
    try:
        return date(ano, mes, dia).isoformat()
    except ValueError:
        return None


class InvoiceItem(BaseModel):
    item_order: int | None = None
    description: str | None = None
    item_code: str | None = Field(
        None,
        description=(
            "Código/SKU do próprio fornecedor para este item, quando a nota tem uma "
            "coluna separada para isso (ex.: 'Item', 'Product', 'SKU', '#') — "
            "diferente da descrição do produto. Não confundir com o UPC."
        ),
    )
    upc: str | None = Field(
        None,
        description=(
            "Código de barras UPC/EAN do produto, quando aparece na nota — em coluna "
            "própria (ex.: 'UPC Item') ou junto da descrição. Só os dígitos."
        ),
    )
    pack_size: str | None = Field(
        None,
        description=(
            "Texto EXATO da coluna de pack/size da linha, quando a nota imprime uma "
            "coluna propria para isso (ex.: '12/12 oz', '6/2LB', '24/16 oz'). Null "
            "quando nao ha essa coluna."
        ),
    )
    quantity: float | None = None
    unit: str | None = None
    unit_price: float | None = None
    total_price: float | None = None
    cases: float | None = Field(
        None,
        description=(
            "Numero de caixas da linha, SO quando a nota imprime uma coluna de "
            "caixas separada da quantidade faturada — comum em carne vendida "
            "por peso variavel (colunas 'CASES' e 'WEIGHT' distintas, pack/size "
            "com sufixo 'AVG', preco por libra). Nesse caso 'quantity'/'unit' "
            "continuam sendo o peso (o que fecha quantity x unit_price = "
            "total_price); 'cases' e so a contagem de caixas, usada para "
            "conferir quantidade pedida x recebida. Deixe null quando a nota "
            "tem uma unica coluna de quantidade (caixa fechada, sem peso "
            "variavel) — nesse caso 'quantity' ja É a caixa."
        ),
    )
    handwritten_code: str | None = Field(
        None,
        description=(
            "Código NUMÉRICO curto escrito à mão na linha (ex.: '40102'), separado "
            "de 'handwritten_notes' — confirmado contra o Catapult real que, para "
            "alguns fornecedores, é o código de catálogo do PRÓPRIO comprador "
            "(scancode/Supplier Unit ID), diferente do código impresso na nota "
            "(que é do catálogo do fornecedor, não bate com o Catapult). Só entra "
            "aqui quando for um número curto isolado que parece código de item — "
            "não uma correção de preço/quantidade nem nota de devolução (isso é "
            "'handwritten_notes'). Deixe null quando não há esse tipo de anotação."
        ),
    )
    handwritten_notes: str | None = Field(
        None,
        description="Anotações manuais em caneta: devoluções, ajustes, correções de preço/quantidade"
    )

    @model_validator(mode="after")
    def _aplicar_multiplicador_de_pack_faltante(self) -> "InvoiceItem":
        """Rede de segurança pra regra 8 do prompt de extração
        (`commons/vision/__init__.py`): se a Vision não preencheu `cases`
        (não aplicou o multiplicador — ou porque não achou o padrão, ou
        porque simplesmente esqueceu nessa linha, o caso comum em notas com
        muitos itens) mas a descrição carrega "<N>X<size>" (ou a coluna de
        pack, `pack_size`, traz "<N>/<size>"), aplica aqui do mesmo jeito: `cases` = quantidade impressa original, `quantity` =
        impressa x N. Não mexe quando `cases` já veio preenchido (regra 7 ou
        regra 8 já aplicadas pela Vision) nem quando não há esse padrão."""
        if self.cases is not None or self.quantity is None:
            return self
        if self.quantity % 1:
            return self  # quantidade fracionada e peso, nao contagem de caixas
        n = _n_do_pack(self.pack_size, self.description)
        if n <= 1:
            return self
        self.cases = self.quantity
        self.quantity = self.quantity * n
        return self


class InvoiceData(BaseModel):
    # ── Invoice ──────────────────────────────────────────────────────────
    invoice_number: str | None = None
    invoice_date: str | None = Field(None, description="ISO 8601: YYYY-MM-DD")
    invoice_date_raw: str | None = Field(
        None, description="Data de emissao exatamente como impressa (ex.: '10/05/26')",
    )
    due_date: str | None = Field(None, description="ISO 8601: YYYY-MM-DD")
    due_date_raw: str | None = Field(
        None, description="Data de vencimento exatamente como impressa",
    )
    currency: str | None = "USD"
    subtotal: float | None = None
    tax_amount: float | None = None
    total_amount: float | None = None

    # ── Fornecedor (Supplier / From) ──────────────────────────────────────
    supplier_name: str | None = None
    supplier_address: str | None = None
    supplier_phone: str | None = None
    supplier_email: str | None = None
    supplier_tax_id: str | None = None

    # ── Cliente (Bill To / Ship To) ───────────────────────────────────────
    client_name: str | None = None
    client_address: str | None = None
    client_tax_id: str | None = None

    # ── Itens ─────────────────────────────────────────────────────────────
    items: list[InvoiceItem] = Field(default_factory=list)

    # ── Anotação manuscrita solta na página (não presa a nenhuma linha) ────
    general_handwritten_notes: str | None = Field(
        None,
        description=(
            "Texto EXATO de qualquer anotação manuscrita na página que não está "
            "presa a nenhuma linha de item específica — ex.: escrita na margem/"
            "espaço em branco abaixo da tabela, um carimbo/nota sobre a nota "
            "inteira. Verbatim, sem comentário nem tradução. Diferente de "
            "'handwritten_notes' de cada item (aquele é só quando a anotação "
            "está claramente ao lado/embaixo de UMA linha). Null quando não há "
            "esse tipo de anotação."
        ),
    )

    # ── Indicador de qualidade da leitura ─────────────────────────────────
    reading_confidence: float = Field(
        ...,
        ge=0,
        le=100,
        description=(
            "Confiança da leitura em % (0-100). "
            "90-100: tudo legível; 70-89: pequenas ambiguidades; "
            "50-69: partes ilegíveis ou muito manuscrito; 0-49: leitura comprometida."
        ),
    )
    reading_status: Literal["success", "partial", "failed"] = "success"
    reading_notes: str | None = Field(
        None,
        description=(
            "Observações em prosa sobre a QUALIDADE da leitura — dificuldades de "
            "OCR, valores incertos, campos não encontrados. NÃO é onde anotação "
            "manuscrita vai — isso é 'handwritten_notes' (por item) ou "
            "'general_handwritten_notes' (solta na página)."
        ),
    )

    # ── Metadados da chamada à API ─────────────────────────────────────────
    model_ai: str = "claude-opus-4-8"
    cost_read: float = Field(
        0.0,
        description="Custo em USD da chamada à API (input + output tokens)",
    )

    @model_validator(mode="after")
    def _converter_datas_impressas(self) -> "InvoiceData":
        """A data impressa (mes/dia/ano nas notas americanas) manda sobre o ISO
        que a Vision converteu. Sem data impressa legivel, fica o ISO da Vision."""
        self.invoice_date = _converter_data_impressa(self.invoice_date_raw) or self.invoice_date
        self.due_date = _converter_data_impressa(self.due_date_raw) or self.due_date
        return self

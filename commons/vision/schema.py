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

from commons.matcher import norm_supplier

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


# "B24"/"C24" = tamanho real da caixa, CONFIRMADO ao vivo contra o Catapult
# real (spec-caixa-letra-florida-distributing) so pra Florida Distributing:
# `Received` do PO = quantidade impressa da invoice x N em 7/7 linhas nao-zeradas
# da invoice 200488064. Fora desse fornecedor a mesma letra+numero pode ser so
# indicador de estoque ambiguo (`spec-qtd-caixa` R3) - por isso o escopo e por
# fornecedor, nunca global. So "24" (unico valor confirmado) - nao um digito
# generico: testado contra a mesma invoice real, "HEINEKEN C2 5L" (um barril,
# "C2" so faz parte do nome do produto) bateria um falso positivo com
# `[BC]\d+` generico. Outro tamanho de caixa deste fornecedor (ex. "B12")
# precisa de confirmacao real antes de entrar aqui.
_PACK_SIZE_LETRA_RE = re.compile(r"\b[BC](24)\b")
_FORNECEDORES_PACK_LETRA = ("FLORIDA DISTRIBUT",)


def _fornecedor_bate(supplier_name: str | None, fornecedores: tuple[str, ...]) -> bool:
    nome = norm_supplier(supplier_name)
    return any(f in nome for f in fornecedores)


# qtd_caixa (spec-qtd-caixa): quantos itens tem em 1 caixa/pack/display, de QUALQUER
# um dos formatos comerciais comuns — campo independente de `cases`, nunca participa
# do multiplicador de `quantity`. So os padroes CLAROS da aba "Quantidades por caixa,
# pack e a[ssemelhados]" da planilha de referencia do cliente; o resto fica `None`
# (nunca adivinha). Tentados nesta ordem, primeiro que bater ganha.
_QTD_SIZE_X_N_RE = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:kg|gr|ml|lb|oz|g|l)\s*x\s*(\d+)\b", re.IGNORECASE
)
_QTD_N_SLASH_SIZE_RE = re.compile(
    r"(\d+)\s*/\s*\d+(?:[.,]\d+)?\s*(?:kg|gr|ml|lbs|lb|oz|g|l|qt|lt|gal)\b", re.IGNORECASE
)
# "PK 100" -> 100; nao "PK 2/100" (composto, ambiguo — negative lookahead de '/').
_QTD_PK_N_RE = re.compile(r"\bPK\s+(\d+)\b(?!\s*/)", re.IGNORECASE)
# "10-Pk", "20PACK", "4 PACK" -> N.
_QTD_N_PACK_RE = re.compile(r"\b(\d+)\s*-?\s*(?:pk|pack)\b", re.IGNORECASE)
# "X12", "BOX X24" -> N. Nunca o 'x' de '12x400g' (lookbehind de digito/ponto) nem
# dimensao tipo '18"x360\'' ou '6X9X3.5' (lookahead de aspas/polegada ou outro x-digito).
_QTD_X_N_RE = re.compile(
    r"(?<![0-9.])\bX\s?(\d+)\b(?!['\"]|\s*[xX]\s*\d)", re.IGNORECASE
)
_QTD_ROLL_PER_BOX_RE = re.compile(r"\b(\d+)\s*rolls?\s*per\s*box\b", re.IGNORECASE)
# "6 pcs", "18 items" soltos no fim da descricao (formato mais comum na pratica:
# fornecedor imprime "<descricao> | <peso> | <N> pcs/items"). N=1 fica de fora
# (minimo 2) — "1 pc" e material de display (shelf talker/wobbler), nao uma
# caixa real de produto, mesmo sendo o literal impresso.
_QTD_N_PCS_ITEMS_RE = re.compile(r"\b(\d+)\s*(?:pcs?|items?)\b", re.IGNORECASE)

_QTD_CAIXA_PATTERNS = (
    (_PACK_SIZE_RE, 1),          # "<N>X<size><UNIT>" ("12x400g" -> 12)
    (_QTD_SIZE_X_N_RE, 1),       # "<size><UNIT> X <N>" ("200GR X15" -> 15)
    (_QTD_N_SLASH_SIZE_RE, 1),   # "<N>/<size><UNIT>" ("24/0.5L" -> 24)
    (_QTD_PK_N_RE, 1),           # "PK <N>" ("PK 100" -> 100)
    (_QTD_N_PACK_RE, 1),         # "<N>-Pk" / "<N>PACK" ("10-Pk", "20PACK" -> N)
    (_QTD_X_N_RE, 1),            # "X<N>" / "BOX X<N>" ("X12", "BOX X24" -> N)
    (_QTD_ROLL_PER_BOX_RE, 1),   # "<N> roll(s) per box" ("1 roll per box" -> 1)
    (_QTD_N_PCS_ITEMS_RE, 2),    # "<N> pcs"/"<N> items" soltos ("18 items" -> 18)
)


def _n_qtd_caixa(pack_size: str | None, description: str | None) -> int:
    """N de quantos itens tem em 1 caixa/pack (spec-qtd-caixa), ou 0 sem nenhum dos
    padroes claros. Procura em `pack_size` e `description` juntos — os formatos
    cobertos aparecem tanto numa coluna propria quanto embutidos na descricao."""
    texto = f"{pack_size or ''} {description or ''}"
    for padrao, minimo in _QTD_CAIXA_PATTERNS:
        achado = padrao.search(texto)
        if achado and int(achado.group(1)) >= minimo:
            return int(achado.group(1))
    return 0

# Data impressa: tres numeros separados por '/', '-' ou '.'. A Vision converte
# a data para ISO "de cabeca" e ja trocou dia/mes/ano em nota americana
# (mes/dia/ano) — a nota saiu da janela da conciliacao e ficou presa.
# A conversao sai da mao do modelo e e feita aqui (spec-data-invoice-mdy).
_PREFIXO_INV_RE = re.compile(r"^INV[-_:\s]+(?=\S)", re.IGNORECASE)
_NUMERO_MR_GOURMET_RE = re.compile(r"^MG-(\d{4}-\d+)$", re.IGNORECASE)
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
    qtd_caixa: float | None = Field(
        None,
        description=(
            "Quantos itens/unidades individuais tem dentro de 1 caixa/pack/display "
            "desta linha, quando a descrição (ou a coluna de pack) traz um formato "
            "comercial claro de contagem — '<N>X<size>' ('12x400g'), '<size> X <N>' "
            "('200GR X15'), '<N>/<size>' ('24/0.5L'), 'PK <N>' ('PK 100'), '<N>-Pk'/"
            "'<N>PACK' ('10-Pk', '20PACK'), 'X<N>'/'BOX X<N>' ('X12', 'BOX X24'), "
            "'<N> roll(s) per box', '<N> pcs'/'<N> items' soltos no fim da descrição "
            "('... | 270 g | 6 pcs' -> 6; '1 pc' sozinho é material de display, não "
            "entra). Captura SÓ o número da contagem, nunca o peso/volume/tamanho. "
            "Campo independente de 'cases': nunca participa do multiplicador de "
            "'quantity'. Deixe null em formato composto/ambíguo ('PK 2/100', 'B24', "
            "'CS 0, PC 2' — indicador de estoque, não pack) ou quando não há nenhum "
            "desses padrões — nunca adivinhe."
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
        description=(
            "Anotações manuais em caneta: devoluções, ajustes, correções de preço/quantidade. "
            "'Insumo' ao lado da linha, ou chave cobrindo várias linhas, entra aqui em cada "
            "linha coberta."
        ),
    )

    # desativado: regra nao multiplica/converte (spec-vision-prompt-enxuto)
    # @model_validator(mode="after")
    # def _aplicar_multiplicador_de_pack_faltante(self) -> "InvoiceItem":
    #     """Rede de segurança pra regra 8 do prompt de extração
    #     (`commons/vision/__init__.py`): se a Vision não preencheu `cases`
    #     (não aplicou o multiplicador — ou porque não achou o padrão, ou
    #     porque simplesmente esqueceu nessa linha, o caso comum em notas com
    #     muitos itens) mas a descrição carrega "<N>X<size>" (ou a coluna de
    #     pack, `pack_size`, traz "<N>/<size>"), aplica aqui do mesmo jeito: `cases` = quantidade impressa original, `quantity` =
    #     impressa x N. Não mexe quando `cases` já veio preenchido (regra 7 ou
    #     regra 8 já aplicadas pela Vision) nem quando não há esse padrão."""
    #     if self.cases is not None or self.quantity is None:
    #         return self
    #     if self.quantity % 1:
    #         return self  # quantidade fracionada e peso, nao contagem de caixas
    #     n = _n_do_pack(self.pack_size, self.description)
    #     if n <= 1:
    #         return self
    #     self.cases = self.quantity
    #     self.quantity = self.quantity * n
    #     return self

    # desativado: regra nao multiplica/converte (spec-vision-prompt-enxuto)
    # @model_validator(mode="after")
    # def _aplicar_qtd_caixa_faltante(self) -> "InvoiceItem":
    #     """Rede de segurança determinística pra `qtd_caixa` (spec-qtd-caixa): se a
    #     Vision deixou null mas a descrição/`pack_size` bate um dos formatos claros
    #     de contagem (`_n_qtd_caixa`), preenche aqui do mesmo jeito que
    #     `_aplicar_multiplicador_de_pack_faltante` faz pra `cases` — mesma lição: o
    #     LLM é inconsistente aplicando regra mecânica em nota com muitas linhas.
    #     Campo isolado: nunca toca `quantity`, `cases` ou `total_price`."""
    #     if self.qtd_caixa is not None:
    #         return self
    #     n = _n_qtd_caixa(self.pack_size, self.description)
    #     if n < 1:
    #         return self
    #     self.qtd_caixa = float(n)
    #     return self

    # desativado: regra nao multiplica/converte (spec-vision-prompt-enxuto)
    # @model_validator(mode="after")
    # def _derivar_cases_de_qtd_caixa(self) -> "InvoiceItem":
    #     """`cases` (spec-cases-derivado-qtd-caixa): quando a regra 8/açougue não
    #     preencheu `cases` mas `quantity` e `qtd_caixa` vieram preenchidos e a
    #     divisão é EXATA (`quantity / qtd_caixa`), deriva `cases` daqui — é o
    #     número de caixas, o mesmo dado que a conciliação ERP já sabe comparar
    #     contra `Ordered` quando o PO está em "Case" (`_comparar_grupo`, guiado
    #     por `po.unit`, nunca por este campo). Divisão não-exata nunca arredonda:
    #     fica `None`, cai no fallback de hoje (`quantity` direto). `qtd_caixa<=1`
    #     também fica de fora — mesmo caso degenerado que a regra 8 já trata como
    #     "não é multiplicador de verdade" (ex. "1x500g" não é um pack real):
    #     dividir por 1 só copiaria `quantity` para `cases` sem informação nova.
    #     `quantity` nunca muda — diferente da regra 8, que multiplica."""
    #     if self.cases is not None or self.quantity is None or self.qtd_caixa is None:
    #         return self
    #     if self.qtd_caixa <= 1 or self.quantity % self.qtd_caixa:
    #         return self
    #     self.cases = self.quantity / self.qtd_caixa
    #     return self


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
            "SÓ a anotação manuscrita com a palavra INSUMO (em qualquer lugar da "
            "página, não presa a uma linha), copiada exatamente como lida — "
            "'IN SUMO', 'Insumo Burger', sem prefixo nem marca. Se NÃO dá para ler "
            "INSUMO mas o manuscrito é uma quase-leitura da palavra ou uma palavra "
            "que não é nome de pessoa (ex.: 'INS BURGER', 'INV BURDER'): "
            "'INSUMO_INCERTO: <o que enxergou>'. A marca só vale nesse caso, nunca "
            "para um INSUMO legível. NUNCA adivinhe, complete ou corrija a palavra. "
            "Assinatura, rubrica, números, 'DONE' e comentários NÃO vão aqui: vão em "
            "'reading_notes'. Null quando não há anotação de insumo."
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
            "OCR, valores incertos, campos não encontrados — e todo manuscrito solto "
            "na página que NÃO seja INSUMO (assinatura, rubrica, 'DONE', comentário). "
            "Anotação de INSUMO vai em 'general_handwritten_notes'; manuscrito preso "
            "a uma linha vai em 'handwritten_notes' do item."
        ),
    )

    # ── Metadados da chamada à API ─────────────────────────────────────────
    model_ai: str = "claude-opus-4-8"
    cost_read: float = Field(
        0.0,
        description="Custo em USD da chamada à API (input + output tokens)",
    )

    @model_validator(mode="after")
    def _tirar_prefixo_inv(self) -> "InvoiceData":
        """'INV-12345' -> '12345'; 'MG-2026-067' -> '2026-067' (spec-invoice-number-prefixo)."""
        if self.invoice_number:
            numero = _PREFIXO_INV_RE.sub("", self.invoice_number.strip())
            self.invoice_number = _NUMERO_MR_GOURMET_RE.sub(lambda m: m.group(1), numero)
        return self

    @model_validator(mode="after")
    def _converter_datas_impressas(self) -> "InvoiceData":
        """A data impressa (mes/dia/ano nas notas americanas) manda sobre o ISO
        que a Vision converteu. Sem data impressa legivel, fica o ISO da Vision."""
        self.invoice_date = _converter_data_impressa(self.invoice_date_raw) or self.invoice_date
        self.due_date = _converter_data_impressa(self.due_date_raw) or self.due_date
        return self

    # desativado: regra nao multiplica/converte (spec-vision-prompt-enxuto)
    # @model_validator(mode="after")
    # def _aplicar_multiplicador_pack_letra_fornecedor(self) -> "InvoiceData":
    #     """"B24"/"C24" = tamanho real da caixa, SO pra Florida Distributing
    #     (spec-caixa-letra-florida-distributing) - mesma formula da regra 8
    #     (`_aplicar_multiplicador_de_pack_faltante`: `cases` = quantidade
    #     impressa, `quantity` = impressa x N), so que o N vem de letra+numero
    #     em vez de "NxSIZE". Escopado por fornecedor de proposito: a mesma
    #     letra+numero noutro fornecedor pode ser so indicador de estoque
    #     ambiguo (`spec-qtd-caixa` R3), entao nunca roda fora daqui."""
    #     if not _fornecedor_bate(self.supplier_name, _FORNECEDORES_PACK_LETRA):
    #         return self
    #     for item in self.items:
    #         if item.cases is not None or item.quantity is None or item.quantity % 1:
    #             continue
    #         achado = _PACK_SIZE_LETRA_RE.search(item.description or "")
    #         if not achado:
    #             continue
    #         n = int(achado.group(1))
    #         if n <= 1:
    #             continue
    #         item.cases = item.quantity
    #         item.quantity = item.quantity * n
    #     return self

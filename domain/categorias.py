"""Categoria de fornecedor — a fonte da verdade, em código.

Mesmo arranjo de `status_exec.py`: o banco guarda o valor, o significado mora
aqui. A coluna é `dim_fornecedor.categoria`.

POR QUE ESTA COLUNA EXISTE

Até aqui, "é fornecedor de carne" não era um fato registrado em lugar nenhum —
era consequência de dois acidentes:

  1. `cotado = true`, marcado à mão por quem cadastrava.
  2. a ausência de alias. Nota de papel ou bebida caía em `supplier_unmapped` e
     era descartada, o que funcionava por não existir de-para para elas.

O acidente (2) deixou de valer quando a conciliação passou a casar fornecedor
por aproximação: um nome parecido o bastante casa sozinho. `Prime Distribution
USA` (mercearia) e `Prime Meats` (carne) estão os dois na base e medem 62.5 —
hoje abaixo do piso, mas a um cadastro de distância de virar erro silencioso.

Com a categoria, escopo vira uma decisão registrada em vez de efeito colateral.

O QUE A CATEGORIA DECIDE — E O QUE ELA NÃO DECIDE

Ela NÃO decide se a nota é processada. Toda nota vai para a conciliação contra
o ERP. O que a categoria decide é se a nota ganha TAMBÉM a comparação contra a
cotação semanal:

    carne          ERP  +  cotação semanal
    tudo o mais    ERP

Por isso `fat_conciliacao` tem `UNIQUE (id_invoice, comparacao)` com
`comparacao IN ('cotacao', 'erp')`: a mesma nota pode ter as duas linhas. Uma
nota de papel não é descartada — ela simplesmente só tem a linha 'erp'.

`eh_cotavel()` responde "entra na comparação por cotação?", nunca "esta nota
interessa?". Tratar não-carne como lixo foi justamente o que o filtro acidental
antigo fazia, e é o que esta coluna existe para corrigir.

CUIDADO AO CLASSIFICAR: `OUTROS` é o padrão de propósito. Fornecedor novo nasce
fora do fluxo de cotação e só entra quando alguém classifica — continua indo
para o ERP nesse meio-tempo. Errar para menos tira a nota da comparação por
cotação, e alguém reclama. Errar para mais compara carne com papel e produz
divergência falsa que ninguém entende.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["CategoriaFornecedor", "CATEGORIAS_COTADAS", "eh_cotavel"]


class CategoriaFornecedor(StrEnum):
    """O que o fornecedor vende. Gravado em `dim_fornecedor.categoria`.

    É um fato sobre o fornecedor, não uma chave de operação — `cotado` continua
    sendo o interruptor de "participa do ciclo desta semana". Misturar os dois
    foi o que produziu a linha `quotation-windermere` em `configs`, com
    ferramenta preenchida e url nula, que quebrava a coleta toda execução.
    """

    CARNE = "carne"
    BEBIDA = "bebida"
    PAPEL = "papel"
    HORTIFRUTI = "hortifruti"
    MERCEARIA = "mercearia"
    INSUMO = "insumo"
    OUTROS = "outros"

    @property
    def descricao(self) -> str:
        return _DESCRICAO[self]


_DESCRICAO: dict[CategoriaFornecedor, str] = {
    CategoriaFornecedor.CARNE: "Proteína — ERP e ciclo de cotação semanal.",
    CategoriaFornecedor.BEBIDA: "Bebidas em geral — só ERP.",
    CategoriaFornecedor.PAPEL: "Descartáveis e material de limpeza — só ERP.",
    CategoriaFornecedor.HORTIFRUTI: "Hortifrúti e perecíveis — só ERP.",
    CategoriaFornecedor.MERCEARIA: "Mercearia seca e distribuição geral — só ERP.",
    CategoriaFornecedor.INSUMO: "Insumo — a nota não é conciliada (não há pedido no Catapult).",
    CategoriaFornecedor.OUTROS: "Não classificado — só ERP, até alguém classificar.",
}


# Categorias que ganham TAMBÉM a comparação contra a cotação semanal. Hoje só
# carne; é aqui que se abre o escopo, não espalhado em cada query.
#
# Não existe lista equivalente para o ERP de propósito: lá entram todas.
CATEGORIAS_COTADAS: frozenset[str] = frozenset({CategoriaFornecedor.CARNE})


def eh_cotavel(categoria: str | None) -> bool:
    """True quando o fornecedor entra TAMBÉM na comparação por cotação.

    False não significa "ignorar a nota" — significa que a comparação dela é a
    do ERP e só ela. Ver o cabeçalho do módulo.

    `None` é falso de propósito: fornecedor sem categoria registrada não entra
    na cotação por omissão; alguém precisa decidir.
    """
    return categoria in CATEGORIAS_COTADAS

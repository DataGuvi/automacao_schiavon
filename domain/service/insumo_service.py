"""Anotacao manuscrita de insumo: nota inteira (texto geral) ou so algumas linhas (spec-anotacao-insumo,
spec-insumo-por-linha). Regra de negocio compartilhada pela coleta e pela conciliacao."""

from __future__ import annotations

from rapidfuzz import fuzz

from commons.matcher import norm_text

_PALAVRA_INSUMO = "insumo"

# Resultado de `classificar_anotacao_insumo`. A Vision marca a duvida com o prefixo
# `INSUMO_INCERTO:` (commons/vision); o fuzzy (>= 70) e a segunda barreira.
INSUMO_SIM = "insumo"
INSUMO_INCERTO = "incerto"
INSUMO_NAO = "nao"
_MARCA_INCERTO = "INSUMOINCERTO"
_LIMIAR_INSUMO = 70
_JANELA_MIN, _JANELA_MAX = 5, 7


def _so_letras(texto: str | None) -> str:
    """Caixa, acento, pontuacao e espacos fora: 'IN SUMO' vira 'INSUMO'."""
    return norm_text(texto).replace(" ", "")


def _parece_insumo(texto: str) -> bool:
    """True se algum trecho de 5 a 7 letras tem similaridade >= 70 com 'insumo'."""
    alvo = _PALAVRA_INSUMO.upper()
    if len(texto) < _JANELA_MIN:
        return len(texto) >= 3 and fuzz.ratio(alvo, texto) >= _LIMIAR_INSUMO
    return any(
        fuzz.ratio(alvo, texto[i:i + tam]) >= _LIMIAR_INSUMO
        for tam in range(_JANELA_MIN, _JANELA_MAX + 1)
        for i in range(len(texto) - tam + 1)
    )


def _bate_insumo(limpo: str) -> bool:
    return _PALAVRA_INSUMO.upper() in limpo or _parece_insumo(limpo)


def classificar_anotacao_insumo(general_handwritten_notes: str | None = None) -> str:
    """`insumo`, `incerto` ou `nao` para a anotação solta da nota (spec-anotacao-insumo).

    Segunda barreira sobre o que a Vision trouxe: o texto geral (a palavra solta na
    margem, ex.: 'Insumo Confeitaria') é conferido contra 'insumo' (palavra inteira ou
    similaridade >= 70) e a marca `INSUMO_INCERTO:` da Vision é descartada antes. Bateu:
    `insumo`. Marca da Vision sem match: `incerto`. Texto sem marca que não bate ('DONE'): `nao`.
    A nota `insumo` não vai ao Catapult (`reconcile_erp_flow._processar_invoice`, antes de `_buscar_po`). Insumo preso
    a linhas é de `separar_itens_insumo`."""
    texto = _so_letras(general_handwritten_notes)
    geral = texto.removeprefix(_MARCA_INCERTO)
    if geral and _bate_insumo(geral):
        return INSUMO_SIM
    if texto.startswith(_MARCA_INCERTO):
        return INSUMO_INCERTO
    return INSUMO_NAO


def separar_itens_insumo(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """`(restantes, insumo)`: linhas cuja anotação à mão é insumo saem da conciliação
    (spec-insumo-por-linha). Texto de linha que não bate com insumo não tira o item."""
    insumo = [i for i in items if _bate_insumo(_so_letras(i.get("handwritten_notes")))]
    ids = {id(i) for i in insumo}
    return [i for i in items if id(i) not in ids], insumo

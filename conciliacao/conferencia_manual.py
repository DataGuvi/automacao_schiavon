"""Invoices que precisam de conferência manual viram pendência na aba
`Conferência Manual` de uma planilha do Google Sheets (separada do
De-Para/Pendentes de `conciliacao/pendentes_sinonimo.py`).

Quando uma invoice fica em "Confiança de leitura baixa", "PO não encontrado"
ou "Produto não identificado" (as 3 categorias que hoje caem na tela
"Conferências manuais" do Grafana), o robô acrescenta uma linha aqui. O
cliente abre a invoice, confere, e marca a coluna `Conferido`. O robô lê isso
de volta e grava `fat_invoice.conferido_manual_em` — ver
`crawler/flow/conferencia_manual_flow.py` e
`domain/service/conciliacao_service.py::marcar_conferido_manual`.

Só acrescenta (`append`), nunca sobrescreve ou apaga. O robô escreve A:D
(Loja, Fornecedor, Nº Invoice, Divergência) e o checkbox `Conferido` (E,
nasce `False`); o cliente só marca a E. Dedupe por `(fornecedor normalizado,
número da invoice normalizado, loja normalizada)` sobre A:C: marcado não
reabre nada (mesmo contrato de `pendentes_sinonimo.py`). `Loja` entra na
chave (pedido do usuário, 2026-10-30) para duas lojas com o mesmo fornecedor
e mesmo número de invoice não se confundirem.

Não toca o banco — só Google Sheets (`commons/sheets`).
"""

from __future__ import annotations

from typing import NamedTuple

from commons.logging_config import get_logger
from commons.matcher import norm_supplier, norm_text
from commons.sheets import SheetsError, append_values, garantir_checkbox, read_values

log = get_logger(__name__)

_ABA = "Conferência Manual"
# Nome da aba entre aspas simples: A1 notation exige isso quando o nome tem
# espaço (sem aspas a API do Sheets nao reconhece o intervalo).
_RANGE = "'Conferência Manual'!A:E"
_COLUNA_CONFERIDO = 4   # E: "Conferido"


class ItemConferencia(NamedTuple):
    """Uma linha pronta para a aba `Conferência Manual`."""

    loja: str
    fornecedor: str
    numero_invoice: str
    motivo: str   # "Divergência" na planilha — na prática é o motivo da revisão


def _chave(loja, fornecedor, numero_invoice) -> tuple[str, str, str]:
    return (norm_supplier(fornecedor), norm_text(str(numero_invoice)), norm_text(str(loja)))


def _marcado(celula) -> bool:
    """Checkbox explicitamente marcado: `True`, "TRUE" ou "VERDADEIRO" (locale pt-BR).

    Célula ausente, vazia ou `False` NÃO conta como marcada.
    """
    return celula is True or str(celula).strip().upper() in ("TRUE", "VERDADEIRO")


def invoices_conferidas(sheet_id: str) -> set[tuple[str, str, str]]:
    """Chaves `(norm_supplier(Fornecedor), norm_text(Nº Invoice), norm_text(Loja))`
    com `Conferido` marcado.

    Lida com `valueRenderOption=UNFORMATTED_VALUE` (checkbox vira bool, sem
    depender de locale). Linha sem a coluna E, vazia ou com `False` fica de
    fora. Propaga `SheetsError` — quem chama decide como reagir (mesmo
    contrato de `pendentes_sinonimo.estado_pendentes`).
    """
    linhas = read_values(sheet_id, _RANGE, render="UNFORMATTED_VALUE")
    conferidas: set[tuple[str, str, str]] = set()
    for row in linhas[1:]:  # pula cabeçalho
        if len(row) < 3 or not norm_supplier(row[1]):
            continue
        if len(row) > _COLUNA_CONFERIDO and _marcado(row[_COLUNA_CONFERIDO]):
            conferidas.add(_chave(row[0], row[1], row[2]))
    return conferidas


def _checkbox_sem_falhar(sheet_id: str, total_linhas: int) -> None:
    """Checkbox é secundário: as linhas já foram gravadas, falha só loga.

    `total_linhas` limita a validação às linhas que existem de fato — faixa
    sem limite faz o Sheets gravar `False` em milhares de linhas em branco
    (ver `commons.sheets.garantir_checkbox`).
    """
    try:
        garantir_checkbox(sheet_id, _ABA, _COLUNA_CONFERIDO, total_linhas)
    except SheetsError:
        log.exception("checkbox da Conferencia Manual nao aplicado")


def sincronizar_pendentes(sheet_id: str | None, novas: list[ItemConferencia]) -> int:
    """Acrescenta `novas` na aba `Conferência Manual`, pulando as que já estão lá.

    Dedupe por `(norm_supplier(fornecedor), norm_text(numero_invoice),
    norm_text(loja))` — mesma normalização usada no resto do motor. `sheet_id`
    ausente -> no-op (feature desligada), retorna 0. Propaga `SheetsError` de
    `commons.sheets` — sincronização é sempre secundária, nunca derruba o
    fluxo principal (mesmo padrão de `pendentes_sinonimo.sincronizar_pendentes`).
    """
    if not sheet_id or not novas:
        return 0

    existentes = read_values(sheet_id, _RANGE)
    ja_vistos = {
        _chave(row[0], row[1], row[2])
        for row in existentes[1:]  # pula cabeçalho
        if len(row) >= 3
    }

    a_gravar = [
        item for item in novas
        if _chave(item.loja, item.fornecedor, item.numero_invoice) not in ja_vistos
    ]
    if not a_gravar:
        return 0

    # Dentro do próprio lote também pode haver repetição — dedupe local antes
    # de gravar (mesma situação de pendentes_sinonimo.py).
    unicas: dict[tuple[str, str, str], ItemConferencia] = {}
    for item in a_gravar:
        chave = _chave(item.loja, item.fornecedor, item.numero_invoice)
        unicas.setdefault(chave, item)

    append_values(
        sheet_id, _RANGE,
        [[item.loja, item.fornecedor, item.numero_invoice, item.motivo, False]
         for item in unicas.values()],
    )
    _checkbox_sem_falhar(sheet_id, len(existentes) + len(unicas))
    return len(unicas)

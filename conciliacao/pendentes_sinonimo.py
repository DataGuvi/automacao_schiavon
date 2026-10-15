"""Itens sem par viram pendência na aba `Pendentes` do Google Sheets.

Quando um item da nota não acha par — nem na cotação semanal
(`IssueCode.NO_QUOTE_FOR_ITEM`), nem no PO do Catapult
(`IssueCode.NO_PO_FOR_ITEM`) — quem decide se falta um sinônimo de vocabulário
é o cliente, não o robô. Este módulo extrai essas linhas do resultado que
`conciliacao/reconcile_quote.py` e `conciliacao/reconcile_erp.py` já produzem
e acrescenta na aba `Pendentes`, pra o cliente ver o que falta e preencher a
aba `De-Para` (que `conciliacao/sinonimos.py` sincroniza de volta).

Só acrescenta (`append`), nunca sobrescreve ou apaga. O robô escreve A:D e o
checkbox "Já resolvi?" (E, nasce `False`); o cliente só marca a E e escreve na
`De-Para`. Dedupe por `(fornecedor normalizado, item normalizado)` sobre A:B:
marcado não reabre nada. A E só é lida por `estado_pendentes`, que libera o
sync do `De-Para` (`conciliacao/sinonimos.py`).

Não toca o banco — só Google Sheets (`commons/sheets`).
"""

from __future__ import annotations

from datetime import date
from typing import NamedTuple

from commons.matcher import norm_item, norm_supplier
from commons.logging_config import get_logger
from commons.sheets import SheetsError, append_values, garantir_checkbox, read_values
from domain.conciliacao_codes import IssueCode

log = get_logger(__name__)

_ABA_PENDENTES = "Pendentes"
_RANGE_PENDENTES = "Pendentes!A:E"
_COLUNA_RESOLVIDO = 4   # E: "Ja resolvi?"

_CODIGO_POR_ORIGEM = {
    "cotacao": IssueCode.NO_QUOTE_FOR_ITEM,
    "erp": IssueCode.NO_PO_FOR_ITEM,
}


class PendenteRow(NamedTuple):
    """Uma linha pronta para a aba `Pendentes`."""

    fornecedor: str
    item: str
    origem: str   # "cotacao" | "erp"
    data: str     # ISO 8601


def extrair_pendentes(
    items: list[dict], fornecedor: str, origem: str, data: date,
) -> list[PendenteRow]:
    """Filtra `items` (mesmo formato que `reconcile_items_against_po` e
    `reconcile_one_invoice` já retornam, com `description_invoice` e
    `issue_codes`) pelas linhas sem par no lado `origem`.

    `origem='cotacao'` -> `IssueCode.NO_QUOTE_FOR_ITEM`.
    `origem='erp'` -> `IssueCode.NO_PO_FOR_ITEM`.
    """
    codigo = _CODIGO_POR_ORIGEM[origem]
    data_iso = data.isoformat()
    return [
        PendenteRow(fornecedor, item["description_invoice"], origem, data_iso)
        for item in items
        if item.get("description_invoice") and codigo in item.get("issue_codes", [])
    ]


def _aberto(celula) -> bool:
    """Checkbox explicitamente desmarcado: `False`, "FALSE" ou "FALSO" (locale pt-BR).

    Célula ausente ou vazia (linha anterior à coluna E) NÃO é aberta.
    """
    return celula is False or str(celula).strip().upper() in ("FALSE", "FALSO")


def estado_pendentes(sheet_id: str) -> tuple[set[str], set[str]]:
    """`(marcados, abertos)`: itens da coluna B da `Pendentes` com a E liberada ou aberta.

    Só `FALSE` explícito é aberto; E marcada, vazia ou ausente (linha antiga) libera.
    Chave `norm_item(B)`, a mesma do DE do `De-Para`. Propaga `SheetsError`.
    """
    linhas = read_values(sheet_id, _RANGE_PENDENTES, render="UNFORMATTED_VALUE")
    marcados: set[str] = set()
    abertos: set[str] = set()
    for row in linhas[1:]:  # pula cabeçalho
        if len(row) < 2 or not norm_item(row[1]):
            continue
        aberta = len(row) > _COLUNA_RESOLVIDO and _aberto(row[_COLUNA_RESOLVIDO])
        (abertos if aberta else marcados).add(norm_item(row[1]))
    return marcados, abertos


def _checkbox_sem_falhar(sheet_id: str) -> None:
    """Checkbox é secundário: as linhas já foram gravadas, falha só loga (`manutencao.preparar_pendentes` refaz)."""
    try:
        garantir_checkbox(sheet_id, _ABA_PENDENTES, _COLUNA_RESOLVIDO)
    except SheetsError:
        log.exception("checkbox da Pendentes nao aplicado")


def sincronizar_pendentes(sheet_id: str | None, novas: list[PendenteRow]) -> int:
    """Acrescenta `novas` na aba `Pendentes`, pulando as que já estão lá.

    Dedupe por `(norm_supplier(fornecedor), norm_item(item))` — mesma
    normalização usada no resto do motor (`commons/matcher.py`), pra não
    depender de grafia idêntica entre execuções. `sheet_id` ausente -> no-op
    (feature desligada), retorna 0. Propaga `SheetsError` de
    `commons.sheets` — quem chama decide como reagir (mesmo padrão de
    `conciliacao/sinonimos.py`: sincronização é sempre secundária, nunca
    derruba o fluxo principal).
    """
    if not sheet_id or not novas:
        return 0

    existentes = read_values(sheet_id, _RANGE_PENDENTES)
    ja_vistos = {
        (norm_supplier(row[0]), norm_item(row[1]))
        for row in existentes[1:]  # pula cabeçalho
        if len(row) >= 2
    }

    a_gravar = [
        p for p in novas
        if (norm_supplier(p.fornecedor), norm_item(p.item)) not in ja_vistos
    ]
    if not a_gravar:
        return 0

    # Dentro do próprio lote também pode haver repetição (duas linhas da
    # mesma invoice sem par pro mesmo item) — dedupe local antes de gravar.
    unicas: dict[tuple[str, str], PendenteRow] = {}
    for p in a_gravar:
        chave = (norm_supplier(p.fornecedor), norm_item(p.item))
        unicas.setdefault(chave, p)

    append_values(
        sheet_id, _RANGE_PENDENTES,
        [[p.fornecedor, p.item, p.origem, p.data, False] for p in unicas.values()],
    )
    _checkbox_sem_falhar(sheet_id)
    return len(unicas)

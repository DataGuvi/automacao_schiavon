"""Leitura e escrita numa planilha do Google Sheets, via Service Account.

Hoje serve a planilha De-Para/Pendentes de sinônimo de item
(`conciliacao/sinonimos.py`, `conciliacao/pendentes_sinonimo.py`), mas o
módulo em si não sabe nada disso — é só transporte genérico (`read_values` /
`append_values`), mesmo papel de `commons/gmail` para a API do Gmail.

Autenticação por Service Account (`resources/google/service_account.json`,
não versionado — ver `.gitignore`), não por OAuth de usuário: é automação sem
supervisão, não precisa de sessão nem de refresh manual. A planilha precisa
estar compartilhada com o `client_email` da Service Account, como Editor.
"""

from __future__ import annotations

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from commons.paths import GOOGLE_SERVICE_ACCOUNT_PATH

__all__ = [
    "read_values", "append_values", "update_values", "garantir_checkbox",
    "inserir_coluna", "set_basic_filter", "SheetsError",
]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class SheetsError(RuntimeError):
    """Falha ao autenticar ou chamar a API do Google Sheets."""


def _service():
    if not GOOGLE_SERVICE_ACCOUNT_PATH.exists():
        raise SheetsError(
            f"'{GOOGLE_SERVICE_ACCOUNT_PATH}' não encontrado. Gere a chave da "
            "Service Account no Google Cloud e salve nesse caminho."
        )
    creds = Credentials.from_service_account_file(
        str(GOOGLE_SERVICE_ACCOUNT_PATH), scopes=SCOPES,
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_values(
    spreadsheet_id: str, range_: str, *, render: str = "FORMATTED_VALUE",
) -> list[list]:
    """Valores de um intervalo (`'De-Para!A:B'`), linha a linha.

    O Sheets omite células vazias no FIM da linha (uma linha só com a coluna A
    preenchida volta como `['foo']`, não `['foo', '']`) — quem consome isto
    precisa tratar linha curta, não assumir todas as colunas presentes.
    `render='UNFORMATTED_VALUE'` devolve checkbox como bool, sem depender de locale.
    """
    try:
        resp = _service().spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=range_, valueRenderOption=render,
        ).execute()
    except HttpError as exc:
        raise SheetsError(f"falha lendo '{range_}' de {spreadsheet_id}: {exc}") from exc
    return resp.get("values", [])


def update_values(spreadsheet_id: str, range_: str, rows: list[list[str]]) -> None:
    """Sobrescreve `range_` com `rows`. Uso pontual (cabecalho), nunca em dado do cliente."""
    try:
        _service().spreadsheets().values().update(
            spreadsheetId=spreadsheet_id, range=range_,
            valueInputOption="RAW", body={"values": rows},
        ).execute()
    except HttpError as exc:
        raise SheetsError(f"falha atualizando '{range_}' de {spreadsheet_id}: {exc}") from exc


def _sheet_id_da_aba(svc, spreadsheet_id: str, aba: str) -> int:
    """`sheetId` numérico (interno do Sheets) de uma aba pelo título dela."""
    meta = svc.get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties(sheetId,title)",
    ).execute()
    ids = [s["properties"]["sheetId"] for s in meta.get("sheets", [])
           if s["properties"]["title"] == aba]
    if not ids:
        raise SheetsError(f"aba '{aba}' nao encontrada em {spreadsheet_id}")
    return ids[0]


def garantir_checkbox(spreadsheet_id: str, aba: str, coluna: int, total_linhas: int) -> None:
    """Checkbox na coluna `coluna` (0 = A) da `aba`, da linha 2 até `total_linhas`
    (1-indexed, inclusive — normalmente cabeçalho + quantidade de dados).
    Idempotente.

    `total_linhas` é OBRIGATÓRIO: uma faixa sem limite de linha (omitir
    `endRowIndex`) faz o Sheets preencher toda linha em branco abaixo dos
    dados com `False` de verdade (não é só validação "visual" — vira
    conteúdo real da célula), o que infla a planilha com milhares de linhas
    fantasma e quebra a detecção de "última linha" que `append_values` usa
    (achado real, 2026-10-30: 274 linhas de dado viraram 1274 na planilha
    Conferência Manual). Quem chama sempre sabe o tamanho atual da tabela
    (acabou de ler ou de gravar nela), então não há motivo para "sem limite".
    """
    try:
        svc = _service().spreadsheets()
        sheet_id = _sheet_id_da_aba(svc, spreadsheet_id, aba)
        svc.batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": [{
            "setDataValidation": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": total_linhas,
                          "startColumnIndex": coluna, "endColumnIndex": coluna + 1},
                "rule": {"condition": {"type": "BOOLEAN"}, "strict": True},
            },
        }]}).execute()
    except HttpError as exc:
        raise SheetsError(f"falha criando checkbox em '{aba}' de {spreadsheet_id}: {exc}") from exc


def inserir_coluna(spreadsheet_id: str, aba: str, indice: int) -> None:
    """Insere uma coluna em branco na `aba`, na posição `indice` (0 = A),
    empurrando as colunas existentes (e os dados delas) para a direita.

    Uso pontual (migração estrutural de planilha já existente) — nunca
    chamado pelo sync de rotina.
    """
    try:
        svc = _service().spreadsheets()
        sheet_id = _sheet_id_da_aba(svc, spreadsheet_id, aba)
        svc.batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": [{
            "insertDimension": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": indice, "endIndex": indice + 1},
                "inheritFromBefore": False,
            },
        }]}).execute()
    except HttpError as exc:
        raise SheetsError(f"falha inserindo coluna em '{aba}' de {spreadsheet_id}: {exc}") from exc


def set_basic_filter(spreadsheet_id: str, aba: str, n_colunas: int) -> None:
    """Liga o filtro básico do Sheets na `aba` inteira (cabeçalho + todas as
    linhas), cobrindo as `n_colunas` primeiras colunas — dá pra filtrar por
    qualquer coluna direto na UI do Sheets. Idempotente (substitui o filtro
    anterior, se havia algum)."""
    try:
        svc = _service().spreadsheets()
        sheet_id = _sheet_id_da_aba(svc, spreadsheet_id, aba)
        svc.batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": [{
            "setBasicFilter": {
                "filter": {"range": {"sheetId": sheet_id, "startRowIndex": 0,
                                      "startColumnIndex": 0, "endColumnIndex": n_colunas}},
            },
        }]}).execute()
    except HttpError as exc:
        raise SheetsError(f"falha ligando filtro em '{aba}' de {spreadsheet_id}: {exc}") from exc


def append_values(spreadsheet_id: str, range_: str, rows: list[list[str | bool]]) -> None:
    """Acrescenta `rows` ao final da tabela em `range_`. Nunca sobrescreve."""
    if not rows:
        return
    try:
        _service().spreadsheets().values().append(
            spreadsheetId=spreadsheet_id, range=range_,
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()
    except HttpError as exc:
        raise SheetsError(f"falha gravando em '{range_}' de {spreadsheet_id}: {exc}") from exc

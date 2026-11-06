"""Testes de commons.sheets.garantir_checkbox / append_values (API mockada, sem rede).

    python -m pytest tests/test_sheets_checkbox.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commons.sheets as sheets  # noqa: E402


def _servico(abas):
    svc = MagicMock()
    ss = svc.spreadsheets.return_value
    ss.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"sheetId": i, "title": t}} for t, i in abas.items()],
    }
    return svc


def test_checkbox_na_coluna_e_da_aba_certa(monkeypatch):
    svc = _servico({"De-Para": 11, "Pendentes": 22})
    monkeypatch.setattr(sheets, "_service", lambda: svc)
    sheets.garantir_checkbox("sheet-1", "Pendentes", 4, 169)
    corpo = svc.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]
    req = corpo["requests"][0]["setDataValidation"]
    assert req["range"] == {"sheetId": 22, "startRowIndex": 1, "endRowIndex": 169,
                            "startColumnIndex": 4, "endColumnIndex": 5}
    assert req["rule"]["condition"]["type"] == "BOOLEAN"


def test_checkbox_aba_inexistente_levanta_sheets_error(monkeypatch):
    monkeypatch.setattr(sheets, "_service", lambda: _servico({"De-Para": 11}))
    try:
        sheets.garantir_checkbox("sheet-1", "Pendentes", 4, 169)
    except sheets.SheetsError as exc:
        assert "Pendentes" in str(exc)
    else:
        raise AssertionError("esperava SheetsError")


def test_append_envia_booleano_cru(monkeypatch):
    svc = MagicMock()
    monkeypatch.setattr(sheets, "_service", lambda: svc)
    sheets.append_values("sheet-1", "Pendentes!A:E", [["a", "b", "erp", "2026-10-15", False]])
    kw = svc.spreadsheets.return_value.values.return_value.append.call_args.kwargs
    assert kw["valueInputOption"] == "RAW"
    assert kw["body"]["values"][0][4] is False


def test_erro_de_rede_vira_sheets_error(monkeypatch):
    svc = MagicMock()
    svc.spreadsheets.return_value.values.return_value.get.return_value.execute.side_effect = TimeoutError("lento")
    monkeypatch.setattr(sheets, "_service", lambda: svc)
    try:
        sheets.read_values("sheet-1", "Pendentes!A:E")
    except sheets.SheetsError as exc:
        assert "lento" in str(exc)
    else:
        raise AssertionError("esperava SheetsError")


def test_remover_linhas_do_maior_para_o_menor(monkeypatch):
    svc = _servico({"Conferência Manual": 7})
    monkeypatch.setattr(sheets, "_service", lambda: svc)
    sheets.remover_linhas("sheet-1", "Conferência Manual", [1, 5, 3])
    reqs = svc.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
    assert [r["deleteDimension"]["range"]["startIndex"] for r in reqs] == [5, 3, 1]

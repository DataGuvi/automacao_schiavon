"""Testes de conciliacao/conferencia_manual.py e das funções de serviço
associadas (domain/service/conciliacao_service.py).

Fixtures em memória, sem rede nem Postgres — spec-conferencia-manual-planilha.

    python -m pytest tests/test_conferencia_manual.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import conciliacao.conferencia_manual as mod  # noqa: E402
from conciliacao.conferencia_manual import (  # noqa: E402
    ItemConferencia,
    invoices_conferidas,
    sincronizar_pendentes,
)
from domain.service.conciliacao_service import (  # noqa: E402
    fetch_invoices_conferencia_manual_pendente,
    marcar_conferido_manual,
)


# ---------------------------------------------------------------------------
# sincronizar_pendentes / invoices_conferidas (commons.sheets fake)
# ---------------------------------------------------------------------------

class _FakeSheet:
    """Substitui commons.sheets.read_values/append_values em memoria."""

    def __init__(self, existentes=None):
        self.existentes = existentes or [["LOJA", "FORNECEDOR", "Nº INVOICE", "DIVERGENCIA", "CONFERIDO"]]
        self.gravados: list[list] = []
        self.checkboxes: list[tuple] = []

    def read_values(self, _sheet_id, _range, **_kw):
        return self.existentes

    def append_values(self, _sheet_id, _range, rows):
        self.gravados.extend(rows)

    def garantir_checkbox(self, _sheet_id, aba, coluna, total_linhas):
        self.checkboxes.append((aba, coluna, total_linhas))


def _instalar_fake(monkeypatch, fake: _FakeSheet):
    monkeypatch.setattr(mod, "read_values", fake.read_values)
    monkeypatch.setattr(mod, "append_values", fake.append_values)
    monkeypatch.setattr(mod, "garantir_checkbox", fake.garantir_checkbox)


def test_sheet_id_ausente_nao_faz_nada(monkeypatch):
    fake = _FakeSheet()
    _instalar_fake(monkeypatch, fake)
    assert sincronizar_pendentes(None, [ItemConferencia("Windermere", "X", "1", "motivo")]) == 0
    assert fake.gravados == []


def test_lista_vazia_nao_faz_nada(monkeypatch):
    fake = _FakeSheet()
    _instalar_fake(monkeypatch, fake)
    assert sincronizar_pendentes("sheet-1", []) == 0
    assert fake.gravados == []


def test_pendente_nova_e_gravada(monkeypatch):
    fake = _FakeSheet()
    _instalar_fake(monkeypatch, fake)
    novas = [ItemConferencia("Windermere", "Cheney Brothers", "12345", "PO nao encontrado no Catapult")]
    n = sincronizar_pendentes("sheet-1", novas)
    assert n == 1
    assert fake.gravados == [["Windermere", "Cheney Brothers", "12345", "PO nao encontrado no Catapult", False]]
    assert fake.gravados[0][4] is False
    assert fake.checkboxes == [("Conferência Manual", 4, 2)]


def test_pendente_ja_existente_nao_duplica(monkeypatch):
    fake = _FakeSheet(existentes=[
        ["LOJA", "FORNECEDOR", "Nº INVOICE", "DIVERGENCIA", "CONFERIDO"],
        ["windermere", "cheney brothers", "12345", "motivo antigo", False],
    ])
    _instalar_fake(monkeypatch, fake)
    novas = [ItemConferencia("Windermere", "Cheney Brothers", "12345", "motivo novo")]
    assert sincronizar_pendentes("sheet-1", novas) == 0
    assert fake.gravados == []


def test_mesma_invoice_em_loja_diferente_nao_e_dedupe(monkeypatch):
    # Loja entra na chave (pedido do usuario, 2026-10-30): mesmo fornecedor e
    # numero de invoice em outra loja e uma pendencia DIFERENTE.
    fake = _FakeSheet(existentes=[
        ["LOJA", "FORNECEDOR", "Nº INVOICE", "DIVERGENCIA", "CONFERIDO"],
        ["Windermere", "Cheney Brothers", "12345", "motivo", False],
    ])
    _instalar_fake(monkeypatch, fake)
    novas = [ItemConferencia("Dr. Phillips", "Cheney Brothers", "12345", "motivo")]
    assert sincronizar_pendentes("sheet-1", novas) == 1
    assert fake.gravados == [["Dr. Phillips", "Cheney Brothers", "12345", "motivo", False]]


def test_dedupe_dentro_do_mesmo_lote(monkeypatch):
    fake = _FakeSheet()
    _instalar_fake(monkeypatch, fake)
    novas = [
        ItemConferencia("Windermere", "Cheney Brothers", "12345", "motivo A"),
        ItemConferencia("windermere", "cheney brothers", "12345", "motivo B"),
    ]
    n = sincronizar_pendentes("sheet-1", novas)
    assert n == 1
    assert len(fake.gravados) == 1


def test_falha_no_checkbox_nao_derruba_a_sincronizacao(monkeypatch):
    from commons.sheets import SheetsError
    fake = _FakeSheet()
    _instalar_fake(monkeypatch, fake)

    def _falha(*_a):
        raise SheetsError("quota")

    monkeypatch.setattr(mod, "garantir_checkbox", _falha)
    novas = [ItemConferencia("Windermere", "Cheney Brothers", "12345", "motivo")]
    assert sincronizar_pendentes("sheet-1", novas) == 1
    assert len(fake.gravados) == 1


def test_invoices_conferidas_so_marcado_explicito_entra(monkeypatch):
    fake = _FakeSheet(existentes=[
        ["LOJA", "FORNECEDOR", "Nº INVOICE", "DIVERGENCIA", "CONFERIDO"],
        ["Windermere", "Fini", "111", "motivo", True],
        ["Windermere", "Fini", "222", "motivo", "VERDADEIRO"],
        ["Windermere", "Fini", "333", "motivo", False],
        ["Windermere", "Fini", "444", "motivo"],       # linha curta (sem E)
        ["Windermere", "Fini"],                        # sem invoice
    ])
    _instalar_fake(monkeypatch, fake)
    from commons.matcher import norm_supplier, norm_text
    conferidas = invoices_conferidas("sheet-1")
    assert conferidas == {
        (norm_supplier("Fini"), norm_text("111"), norm_text("Windermere")),
        (norm_supplier("Fini"), norm_text("222"), norm_text("Windermere")),
    }


# ---------------------------------------------------------------------------
# marcar_conferido_manual (fake conn, mesmo padrao de test_po_fechado_manual.py)
# ---------------------------------------------------------------------------

class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.sqls.append((sql, params))
        self._ultimo = sql
        if sql.lstrip().startswith("UPDATE"):
            self.rowcount = len(params[0])

    def fetchall(self):
        return self.conn.candidatas


class _Conn:
    def __init__(self, candidatas):
        self.candidatas, self.sqls, self.commits = candidatas, [], 0

    def cursor(self, **_kw):
        return _Cursor(self)

    def commit(self):
        self.commits += 1


def _updates(conn):
    return [(s, p) for s, p in conn.sqls if s.lstrip().startswith("UPDATE")]


def test_marcar_conferido_manual_sem_chave_nao_faz_nada():
    conn = _Conn(candidatas=[{"id": 1, "nome_fornecedor": "Fini", "numero_invoice": "111", "loja": "Windermere"}])
    assert marcar_conferido_manual(conn, set()) == 0
    assert conn.sqls == []


def test_marcar_conferido_manual_casa_por_fornecedor_numero_e_loja():
    from commons.matcher import norm_supplier, norm_text
    conn = _Conn(candidatas=[
        {"id": 1, "nome_fornecedor": "Fini", "numero_invoice": "111", "loja": "Windermere"},
        {"id": 2, "nome_fornecedor": "Outro Fornecedor", "numero_invoice": "999", "loja": "Dr. Phillips"},
    ])
    conferidas = {(norm_supplier("Fini"), norm_text("111"), norm_text("Windermere"))}
    n = marcar_conferido_manual(conn, conferidas)
    assert n == 1
    ups = _updates(conn)
    assert len(ups) == 1 and ups[0][1] == ([1],)
    assert conn.commits == 1


def test_marcar_conferido_manual_loja_diferente_nao_casa():
    # Mesmo fornecedor/numero, loja diferente: nao e a mesma invoice.
    from commons.matcher import norm_supplier, norm_text
    conn = _Conn(candidatas=[
        {"id": 1, "nome_fornecedor": "Fini", "numero_invoice": "111", "loja": "Windermere"},
    ])
    conferidas = {(norm_supplier("Fini"), norm_text("111"), norm_text("Dr. Phillips"))}
    assert marcar_conferido_manual(conn, conferidas) == 0
    assert _updates(conn) == [] and conn.commits == 0


def test_marcar_conferido_manual_nenhuma_candidata_casa():
    from commons.matcher import norm_supplier, norm_text
    conn = _Conn(candidatas=[{"id": 1, "nome_fornecedor": "Fini", "numero_invoice": "111", "loja": "Windermere"}])
    conferidas = {(norm_supplier("Outro"), norm_text("222"), norm_text("Windermere"))}
    assert marcar_conferido_manual(conn, conferidas) == 0
    assert _updates(conn) == [] and conn.commits == 0


# ---------------------------------------------------------------------------
# fetch_invoices_conferencia_manual_pendente — so garante que o SELECT roda e
# o resultado do driver passa direto (a condicao SQL em si so e validada
# contra o Postgres real, como o resto das queries do Grafana).
# ---------------------------------------------------------------------------

class _SelectCursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self.rows


class _SelectConn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self, **_kw):
        return _SelectCursor(self.rows)


def test_fetch_invoices_conferencia_manual_pendente_filtra_pelo_inicio_da_operacao():
    from domain.service.conciliacao_service import INICIO_OPERACAO
    from domain.enums import StatusExecucao

    recebido = []

    class _Cur(_SelectCursor):
        def execute(self, sql, params=None):
            recebido.append(params)

    class _Conn(_SelectConn):
        def cursor(self, **_kw):
            return _Cur(self.rows)

    fetch_invoices_conferencia_manual_pendente(_Conn([]))
    assert recebido == [(INICIO_OPERACAO, (int(StatusExecucao.PO_NAO_ENCONTRADA),))]


def test_linhas_a_remover_poupa_cabecalho_e_invoices_da_operacao():
    from conciliacao.conferencia_manual import _chave
    from manutencao.limpar_conferencia_manual_antes_operacao import linhas_a_remover

    linhas = [
        ["Loja", "Fornecedor", "Nº Invoice", "Divergência", "Conferido"],
        ["Windermere", "Fini", "111", "PO nao encontrado", False],
        ["Windermere", "Fini", "222", "PO nao encontrado", False],
        [],
    ]
    ficam = {_chave("Windermere", "Fini", "222")}
    assert linhas_a_remover(linhas, ficam) == [1, 3]


def test_fetch_invoices_conferencia_manual_pendente_devolve_as_linhas():
    conn = _SelectConn([{"loja": "Windermere", "fornecedor": "Fini", "numero_invoice": "111",
                          "motivo": "PO nao encontrado"}])
    rows = fetch_invoices_conferencia_manual_pendente(conn)
    assert rows == [{"loja": "Windermere", "fornecedor": "Fini", "numero_invoice": "111",
                      "motivo": "PO nao encontrado"}]

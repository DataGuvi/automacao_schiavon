"""Spec spec-po-fechado-manual: PO fechado a mao -> itens identificados e nota finalizada.

Fixtures em memoria, sem Postgres nem Catapult.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.flow import reconcile_erp_flow as flow  # noqa: E402
from domain.service.conciliacao_service import marcar_itens_po_fechado  # noqa: E402
from tests.test_reconcile_erp import _item  # noqa: E402


# --- fluxo: _processar_invoice ------------------------------------------------

def _rodar(monkeypatch, *, tentativas_esgotadas, ja_conciliada, trocados=(1, False)):
    ev = {"concluiu": [], "gravou": [], "marcou": []}
    monkeypatch.setattr(flow, "_buscar_po", lambda *a, **k: None)
    monkeypatch.setattr(flow.proc, "aguardar_po_ordered", lambda *a, **k: not tentativas_esgotadas)
    monkeypatch.setattr(flow, "ja_conciliada_erp", lambda *a, **k: ja_conciliada)
    monkeypatch.setattr(flow, "marcar_itens_po_fechado", lambda c, i: ev["marcou"].append(i) or trocados)
    monkeypatch.setattr(flow.proc, "concluir_etapa", lambda *a, **k: ev["concluiu"].append(k))
    monkeypatch.setattr(flow, "_gravar_resultado", lambda *a, **k: ev["gravou"].append(a))
    totais = {"aguardando_po": 0, "sem_po": 0, "po_fechado_manual": 0}
    header = {"id": 1, "id_processo": 9, "invoice_number": "N1", "supplier_name": "Acme"}
    items = [_item(1, "Chicken Breast", "20", "2.5", "50", item_code="X1")]
    flow._processar_invoice(None, "u", None, header, items, {}, lambda n: None, {}, None, totais)
    return totais, ev


def test_terceira_tentativa_com_nota_ja_conciliada_finaliza(monkeypatch):
    totais, ev = _rodar(monkeypatch, tentativas_esgotadas=True, ja_conciliada=True)
    assert ev["marcou"] == [1]
    assert ev["concluiu"] == [{"com_alerta": False}]
    assert totais["po_fechado_manual"] == 1 and totais["sem_po"] == 0
    assert ev["gravou"] == []          # sem relatorio "nenhum PO Ordered" nem e-mail


def test_outra_divergencia_finaliza_com_alerta(monkeypatch):
    _, ev = _rodar(monkeypatch, tentativas_esgotadas=True, ja_conciliada=True, trocados=(2, True))
    assert ev["concluiu"] == [{"com_alerta": True}]


def test_ainda_dentro_das_tentativas_segue_aguardando(monkeypatch):
    totais, ev = _rodar(monkeypatch, tentativas_esgotadas=False, ja_conciliada=True)
    assert totais["aguardando_po"] == 1 and ev["marcou"] == [] and ev["concluiu"] == []


def test_nota_sem_conciliacao_previa_segue_fluxo_atual(monkeypatch):
    totais, ev = _rodar(monkeypatch, tentativas_esgotadas=True, ja_conciliada=False)
    assert ev["marcou"] == [] and totais["sem_po"] == 1 and len(ev["gravou"]) == 1


def test_sem_item_no_po_for_item_segue_fluxo_atual(monkeypatch):
    totais, ev = _rodar(monkeypatch, tentativas_esgotadas=True, ja_conciliada=True, trocados=(0, False))
    assert ev["concluiu"] == [] and totais["sem_po"] == 1 and len(ev["gravou"]) == 1


# --- servico: marcar_itens_po_fechado -------------------------------------------

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

    def fetchall(self):
        return self.conn.itens if "fat_conciliacao_item" in self._ultimo else self.conn.cab


class _Conn:
    def __init__(self, itens, cab):
        self.itens, self.cab, self.sqls, self.commits = itens, cab, [], 0

    def cursor(self, **_kw):
        return _Cursor(self)

    def commit(self):
        self.commits += 1


def _updates(conn):
    return [(s, p) for s, p in conn.sqls if s.lstrip().startswith("UPDATE")]


def test_troca_so_o_item_sem_po_e_recalcula_o_veredito():
    conn = _Conn(
        itens=[{"id": 10, "issue_codes": ["no_po_for_item"], "revisar": False, "match_nivel": "unmatched"},
               {"id": 11, "issue_codes": ["qty_mismatch_po"], "revisar": False, "match_nivel": "codigo"}],
        cab=[{"id": 5, "issue_codes": ["no_po_for_item", "qty_mismatch_po"]}],
    )
    assert marcar_itens_po_fechado(conn, 1) == (1, True)       # sobrou divergencia: com alerta
    ups = _updates(conn)
    assert len(ups) == 2 and conn.commits == 1
    assert ups[0][1] == (["po_fechado_manual"], 0, "CONFERIDO", 10)
    assert "po_fechado_manual" in ups[1][1][0] and "no_po_for_item" not in ups[1][1][0]
    assert ups[1][1][3] == 5


def test_todos_os_itens_identificados_nao_sobra_alerta():
    conn = _Conn(
        itens=[{"id": 10, "issue_codes": ["no_po_for_item"], "revisar": False, "match_nivel": "unmatched"},
               {"id": 11, "issue_codes": [], "revisar": False, "match_nivel": "nome"}],
        cab=[{"id": 5, "issue_codes": ["no_po_for_item"]}],
    )
    assert marcar_itens_po_fechado(conn, 1) == (1, False)


def test_sem_item_no_po_for_item_nao_grava_nada():
    conn = _Conn(
        itens=[{"id": 11, "issue_codes": ["qty_mismatch_po"], "revisar": False, "match_nivel": "codigo"}],
        cab=[{"id": 5, "issue_codes": ["qty_mismatch_po"]}],
    )
    assert marcar_itens_po_fechado(conn, 1) == (0, False)
    assert _updates(conn) == [] and conn.commits == 0


def test_nenhum_item_casou_antes_nao_prova_que_o_po_existiu():
    # Todos 'unmatched': alias errado ou busca falha, nao PO fechado a mao.
    conn = _Conn(
        itens=[{"id": 10, "issue_codes": ["no_po_for_item"], "revisar": False, "match_nivel": "unmatched"}],
        cab=[{"id": 5, "issue_codes": ["no_po_for_item"]}],
    )
    assert marcar_itens_po_fechado(conn, 1) == (0, False)
    assert _updates(conn) == [] and conn.commits == 0


def test_cabecalho_fica_coerente_com_os_itens():
    # O cabecalho e a uniao dos codigos dos itens (reconcile_erp.py): continua divergente.
    conn = _Conn(
        itens=[{"id": 10, "issue_codes": ["no_po_for_item"], "revisar": False, "match_nivel": "unmatched"},
               {"id": 11, "issue_codes": ["price_mismatch_po"], "revisar": False, "match_nivel": "nome"}],
        cab=[{"id": 5, "issue_codes": ["no_po_for_item", "price_mismatch_po"]}],
    )
    marcar_itens_po_fechado(conn, 1)
    cab = _updates(conn)[-1][1]
    assert sorted(cab[0]) == ["po_fechado_manual", "price_mismatch_po"]
    assert cab[2] == "DIVERGENCIA"

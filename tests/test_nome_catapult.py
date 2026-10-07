from crawler.flow import reconcile_erp_flow as flow
from domain.service.conciliacao_service import (
    escolher_nome_catapult, montar_resolvedor_fornecedor,
)

FORNECEDORES = [
    {"id": 42, "nome": "Perdomo Distributor", "nome_catapult": "Perdomo"},
    {"id": 7, "nome": "FreshPoint", "nome_catapult": "Fresh Poin"},
]


def test_fuzzy_contra_nome_canonico():
    resolver = montar_resolvedor_fornecedor(FORNECEDORES, {})
    assert resolver("Perdomo Distributor")["id"] == 42


def test_alias_invoice_exato_tem_prioridade():
    aliases = {"FRESHPOINT CENTRAL FL": {"canonical_id": 7}}
    resolver = montar_resolvedor_fornecedor(FORNECEDORES, aliases)
    assert resolver("Freshpoint Central FL")["id"] == 7


def test_sem_cadastro_devolve_none():
    resolver = montar_resolvedor_fornecedor(FORNECEDORES, {})
    assert resolver("Fornecedor Desconhecido LLC") is None


def test_escolher_nome_catapult_exige_95():
    assert escolher_nome_catapult("Perdomo Distributor", ["Perdomo", "Mena Impor"]) == "Perdomo"
    assert escolher_nome_catapult("Perdomo Distributor", ["Mena Impor"]) is None


def test_aprender_grava_quando_difere(monkeypatch):
    gravados = []
    monkeypatch.setattr(flow, "gravar_nome_catapult", lambda *a: gravados.append(a) or True)
    forn = {"id": 42, "nome": "Perdomo Distributor", "nome_catapult": None}
    flow._aprender_nome_catapult(
        object(), forn, "Perdomo Distributor", [{"name": "Perdomo-036998-HQ-RS2"}],
    )
    assert gravados[0][1:] == (42, "Perdomo") and forn["nome_catapult"] == "Perdomo"


def test_aprender_nao_grava_quando_igual(monkeypatch):
    gravados = []
    monkeypatch.setattr(flow, "gravar_nome_catapult", lambda *a: gravados.append(a) or True)
    forn = {"id": 42, "nome": "Perdomo Distributor", "nome_catapult": "Perdomo"}
    flow._aprender_nome_catapult(
        object(), forn, "Perdomo Distributor", [{"name": "Perdomo-1"}],
    )
    assert gravados == []


def test_aprender_nao_grava_se_fornecedor_resolvido_for_fraco(monkeypatch):
    gravados = []
    monkeypatch.setattr(flow, "gravar_nome_catapult", lambda *a: gravados.append(a) or True)
    forn = {"id": 9, "nome": "Prime Distribution USA", "nome_catapult": None}
    flow._aprender_nome_catapult(object(), forn, "Prime Meats", [{"name": "Prime Meats-1"}])
    assert gravados == []


class _Cur:
    def __init__(self, dono, rowcount=0):
        self.dono, self.rowcount, self.sqls = dono, rowcount, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sqls.append(sql)

    def fetchone(self):
        return self.dono


class _Conn:
    def __init__(self, cur):
        self.cur, self.commits = cur, 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


def test_gravar_nao_reaponta_alias_de_outro_fornecedor():
    from domain.service.conciliacao_service import gravar_nome_catapult
    cur = _Cur(dono=(99,))
    assert gravar_nome_catapult(_Conn(cur), 42, "Perdomo") is False
    assert len(cur.sqls) == 1


def test_gravar_insere_quando_fornecedor_nao_tem_alias_erp():
    from domain.service.conciliacao_service import gravar_nome_catapult
    cur = _Cur(dono=None, rowcount=0)
    conn = _Conn(cur)
    assert gravar_nome_catapult(conn, 42, "Perdomo") is True
    assert "INSERT" in cur.sqls[-1] and conn.commits == 1

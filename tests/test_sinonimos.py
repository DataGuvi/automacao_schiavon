"""Testes do de-para de sinonimos de item (conciliacao/sinonimos.py).

Fixtures em memoria — nao tocam o Postgres nem a rede. A leitura do Sheets e
sempre injetada (`_leitor`), nunca uma chamada real a API.

    python -m pytest tests/test_sinonimos.py -v
    python tests/test_sinonimos.py          (sem pytest instalado)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from commons.sheets import SheetsError  # noqa: E402
from domain.service.conciliacao_service import upsert_item_sinonimos  # noqa: E402
from conciliacao.sinonimos import (  # noqa: E402
    Sinonimo,
    sincronizar,
    _sinonimos_de_linhas,
)

_SHEET_ID = "fake-sheet-id"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _linhas(rows, header=("DE", "PARA")):
    """Mesmo formato que `commons.sheets.read_values` devolve: list[list[str]],
    só a célula vazia no FIM da linha vem omitida (interior preserva `None`)."""
    linhas = [list(header)] if header is not None else []
    for r in rows:
        row = list(r)
        while row and row[-1] is None:
            row.pop()
        linhas.append(row)
    return linhas


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def executemany(self, sql, seq):
        self.conn.execs.append((sql, list(seq)))

    def execute(self, sql, *a):
        self._last = sql

    def fetchall(self):
        # fetch_item_sinonimos espera pares (termo_norm, canonico)
        return list(self.conn.sinonimos_no_banco.items())

    def fetchone(self):
        # _contar (marcar_reprocesso_por_vocabulario) espera (n,)
        return (self.conn.notas_para_reprocesso,)


class _FakeConn:
    """Registra os executemany; o suficiente para upsert_item_sinonimos e para
    a deteccao de mudanca de vocabulario (fetch_item_sinonimos / _contar)."""

    def __init__(self, sinonimos_no_banco=None, notas_para_reprocesso=0):
        self.execs: list = []
        self.sinonimos_no_banco = dict(sinonimos_no_banco or {})
        self.notas_para_reprocesso = notas_para_reprocesso

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        pass


# ---------------------------------------------------------------------------
# Leitura / validacao das linhas (fonte-agnostico)
# ---------------------------------------------------------------------------

def test_le_de_para_valido():
    linhas = _linhas([("Chix", "Chicken"), ("Sassami", "Tender"), ("Boi", "Beef")])
    rows, rel = _sinonimos_de_linhas(linhas)
    assert rel.validas == 3
    assert rel.invalidas == 0 and rel.duplicadas == 0
    assert Sinonimo("Chix", "CHIX", "CHICKEN") in rows


def test_sem_coluna_obrigatoria_falha():
    linhas = _linhas([("Chix", "Chicken")], header=("DE", "OUTRA"))
    try:
        _sinonimos_de_linhas(linhas)
    except ValueError as exc:
        assert "PARA" in str(exc)
    else:
        raise AssertionError("esperava ValueError por coluna faltando")


def test_colunas_em_qualquer_ordem():
    linhas = _linhas([("Chicken", "Chix")], header=("PARA", "DE"))
    rows, rel = _sinonimos_de_linhas(linhas)
    assert rows == [Sinonimo("Chix", "CHIX", "CHICKEN")]
    assert rel.validas == 1


def test_linha_vazia_e_ignorada_sem_contar_como_invalida():
    linhas = _linhas([("Chix", "Chicken"), (None, None), ("Sassami", "Tender")])
    rows, rel = _sinonimos_de_linhas(linhas)
    assert rel.lidas == 2 and rel.validas == 2 and rel.invalidas == 0


def test_linha_curta_do_sheets_conta_como_um_lado_so():
    # Sheets omite celula vazia no FIM da linha: ('Chix',) em vez de ('Chix', None).
    linhas = [["DE", "PARA"], ["Chix"]]
    rows, rel = _sinonimos_de_linhas(linhas)
    assert rows == []
    assert rel.invalidas == 1
    assert "um lado" in rel.descartes[0][1]


def test_linha_com_um_lado_so_e_invalida():
    linhas = _linhas([("Chix", None), (None, "Chicken")])
    rows, rel = _sinonimos_de_linhas(linhas)
    assert rows == []
    assert rel.invalidas == 2
    assert all("um lado" in motivo for _, motivo in rel.descartes)


def test_termo_multi_token_e_valido():
    # DE pode ser a descricao inteira; norm_item traduz a frase toda.
    linhas = _linhas([("Tri Tip", "Maminha"), ("Chix", "Chicken")])
    rows, rel = _sinonimos_de_linhas(linhas)
    assert [s.termo_norm for s in rows] == ["TRI TIP", "CHIX"]
    assert rel.invalidas == 0


def test_cauda_de_catalogo_sai_do_de_e_do_para():
    linhas = _linhas([("Fini G Ovo Frito - FINI, Item #2914", "Fini Fried Eggs - 40622")])
    rows, _ = _sinonimos_de_linhas(linhas)
    assert rows == [Sinonimo("Fini G Ovo Frito - FINI, Item #2914",
                             "FINI G OVO FRITO", "FINI FRIED EGGS")]


def test_duplicidade_no_arquivo_ultima_vence():
    linhas = _linhas([("Chix", "Chicken"), ("chix", "Poultry")])
    rows, rel = _sinonimos_de_linhas(linhas)
    assert rel.validas == 1 and rel.duplicadas == 1
    assert rows[0] == Sinonimo("chix", "CHIX", "POULTRY")


def test_normalizacao_acento_e_nbsp():
    linhas = _linhas([("Fígado", "Liver"), ("Coração\xa0", "Hearts")])
    rows, _ = _sinonimos_de_linhas(linhas)
    assert {s.termo_norm for s in rows} == {"FIGADO", "CORACAO"}


def test_leitura_e_idempotente():
    linhas = _linhas([("Chix", "Chicken"), ("Sassami", "Tender")])
    a, _ = _sinonimos_de_linhas(linhas)
    b, _ = _sinonimos_de_linhas(linhas)
    assert a == b


def test_aba_vazia_falha():
    try:
        _sinonimos_de_linhas([])
    except ValueError as exc:
        assert "vazia" in str(exc)
    else:
        raise AssertionError("esperava ValueError para aba sem nem cabecalho")


# ---------------------------------------------------------------------------
# UPSERT
# ---------------------------------------------------------------------------

def test_upsert_sql_e_on_conflict():
    fake = _FakeConn()
    upsert_item_sinonimos(fake, [Sinonimo("Chix", "CHIX", "CHICKEN")])
    sql, seq = fake.execs[0]
    assert "ON CONFLICT (termo_norm)" in sql
    assert seq == [("Chix", "CHIX", "CHICKEN")]


def test_upsert_vazio_nao_toca_o_banco():
    fake = _FakeConn()
    assert upsert_item_sinonimos(fake, []) == 0
    assert fake.execs == []


# ---------------------------------------------------------------------------
# Sincronizacao (preambulo do pipeline)
# ---------------------------------------------------------------------------

def _SEM_PENDENTES(_sheet_id):
    return set(), set()


def _leitor_fixo(linhas):
    """`_leitor` fake: ignora o sheet_id, devolve sempre as mesmas linhas."""
    def _ler(_sheet_id):
        return _sinonimos_de_linhas(linhas)
    return _ler


def test_sincronizar_grava_via_upsert_e_e_idempotente():
    leitor = _leitor_fixo(_linhas([("Chix", "Chicken"), ("Sassami", "Tender")]))

    fake = _FakeConn()
    sincronizar(fake, _SHEET_ID, _leitor=leitor, _estado=_SEM_PENDENTES)
    sincronizar(fake, _SHEET_ID, _leitor=leitor, _estado=_SEM_PENDENTES)

    assert len(fake.execs) == 2
    assert fake.execs[0][1] == fake.execs[1][1]          # mesmo payload nas duas
    assert sorted(fake.execs[0][1]) == [
        ("Chix", "CHIX", "CHICKEN"), ("Sassami", "SASSAMI", "TENDER"),
    ]


def test_sincronizar_so_reprocessa_quando_vocabulario_muda():
    import domain.service.conciliacao_service as cdb

    chamadas: list = []
    original = cdb.marcar_reprocesso_por_vocabulario
    cdb.marcar_reprocesso_por_vocabulario = lambda conn: chamadas.append(1) or 0
    try:
        leitor = _leitor_fixo(_linhas([("Chix", "Chicken"), ("Sassami", "Tender")]))

        # Banco já com o mesmo de-para: nada mudou -> não remarca.
        ja_igual = {"CHIX": "CHICKEN", "SASSAMI": "TENDER"}
        sincronizar(_FakeConn(sinonimos_no_banco=ja_igual), _SHEET_ID, _leitor=leitor, _estado=_SEM_PENDENTES)
        assert chamadas == []

        # Banco com destino diferente para SASSAMI: mudou -> remarca uma vez.
        difere = {"CHIX": "CHICKEN", "SASSAMI": "STRIP"}
        sincronizar(_FakeConn(sinonimos_no_banco=difere), _SHEET_ID, _leitor=leitor, _estado=_SEM_PENDENTES)
        assert chamadas == [1]
    finally:
        cdb.marcar_reprocesso_por_vocabulario = original


def test_sincronizar_retem_de_pendente_aberta_e_libera_a_marcada():
    leitor = _leitor_fixo(_linhas([("Chix", "Chicken"), ("Sassami", "Tender"), ("Boi", "Beef")]))
    # CHIX: pendente marcada (libera); SASSAMI: pendente aberta (retido); BOI: sem pendente (entra).
    estado = lambda _id: ({"CHIX"}, {"SASSAMI"})  # noqa: E731
    fake = _FakeConn()
    rel = sincronizar(fake, _SHEET_ID, _leitor=leitor, _estado=estado)
    assert sorted(fake.execs[0][1]) == [("Boi", "BOI", "BEEF"), ("Chix", "CHIX", "CHICKEN")]
    assert rel.retidas == 1


def test_sincronizar_item_com_varias_pendentes_basta_uma_marcada():
    leitor = _leitor_fixo(_linhas([("Chix", "Chicken")]))
    fake = _FakeConn()
    rel = sincronizar(fake, _SHEET_ID, _leitor=leitor, _estado=lambda _id: ({"CHIX"}, {"CHIX"}))
    assert fake.execs[0][1] == [("Chix", "CHIX", "CHICKEN")]
    assert rel.retidas == 0


def test_sincronizar_pula_quando_pendentes_indisponivel():
    def _sem_pendentes(_sheet_id):
        raise SheetsError("aba Pendentes renomeada")

    fake = _FakeConn()
    leitor = _leitor_fixo(_linhas([("Chix", "Chicken")]))
    assert sincronizar(fake, _SHEET_ID, _leitor=leitor, _estado=_sem_pendentes) is None
    assert fake.execs == []          # rascunho nao vaza


def test_sincronizar_pula_quando_sheets_indisponivel():
    def _indisponivel(_sheet_id):
        raise SheetsError("credencial ausente")

    fake = _FakeConn()
    rel = sincronizar(fake, _SHEET_ID, _leitor=_indisponivel, _estado=_SEM_PENDENTES)
    assert rel is None
    assert fake.execs == []          # nada gravado, pipeline segue


def test_sincronizar_pula_quando_aba_invalida():
    def _sem_coluna(_sheet_id):
        return _sinonimos_de_linhas(_linhas([("Chix", "Chicken")], header=("DE", "OUTRA")))

    fake = _FakeConn()
    rel = sincronizar(fake, _SHEET_ID, _leitor=_sem_coluna, _estado=_SEM_PENDENTES)
    assert rel is None
    assert fake.execs == []


def test_sincronizar_sem_sheet_id_retorna_none():
    fake = _FakeConn()
    assert sincronizar(fake, None) is None
    assert sincronizar(fake, "") is None
    assert fake.execs == []


def test_sincronizar_dry_run_nao_grava():
    leitor = _leitor_fixo(_linhas([("Chix", "Chicken")]))
    fake = _FakeConn()
    rel = sincronizar(fake, _SHEET_ID, _leitor=leitor, _estado=_SEM_PENDENTES, aplicar=False)
    assert rel is not None
    assert fake.execs == []


if __name__ == "__main__":
    falhas = 0
    testes = [(n, o) for n, o in sorted(globals().items())
              if n.startswith("test_") and callable(o)]
    for nome, func in testes:
        try:
            func()
            print(f"  ok   {nome}")
        except AssertionError as exc:
            falhas += 1
            print(f"  FALHA {nome}: {exc or 'assert'}")
        except Exception as exc:  # noqa: BLE001
            falhas += 1
            print(f"  ERRO  {nome}: {type(exc).__name__}: {exc}")
    print(f"\n{len(testes) - falhas}/{len(testes)} passaram")
    sys.exit(1 if falhas else 0)

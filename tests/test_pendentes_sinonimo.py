"""Testes de conciliacao/pendentes_sinonimo.py.

Fixtures em memoria, sem rede — `sincronizar_pendentes` e testado com
`commons.sheets.read_values`/`append_values` trocados por fakes.

    python -m pytest tests/test_pendentes_sinonimo.py -v
    python tests/test_pendentes_sinonimo.py          (sem pytest instalado)
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import conciliacao.pendentes_sinonimo as mod  # noqa: E402
from conciliacao.pendentes_sinonimo import (  # noqa: E402
    PendenteRow,
    estado_pendentes,
    extrair_pendentes,
    sincronizar_pendentes,
)

_DATA = date(2026, 6, 25)


# ---------------------------------------------------------------------------
# extrair_pendentes
# ---------------------------------------------------------------------------

def test_item_sem_par_na_cotacao_entra():
    items = [{"description_invoice": "BEEF SIRLOIN", "issue_codes": ["no_quote_for_item"]}]
    pend = extrair_pendentes(items, "Cheney Brothers", "cotacao", _DATA)
    assert pend == [PendenteRow("Cheney Brothers", "BEEF SIRLOIN", "cotacao", "2026-06-25")]


def test_item_sem_par_no_po_entra():
    items = [{"description_invoice": "Pocos de Caldas Requeijao", "issue_codes": ["no_po_for_item"]}]
    pend = extrair_pendentes(items, "Triunfo Foods", "erp", _DATA)
    assert pend == [PendenteRow("Triunfo Foods", "Pocos de Caldas Requeijao", "erp", "2026-06-25")]


def test_item_com_outro_issue_code_nao_entra():
    items = [{"description_invoice": "BEEF SIRLOIN", "issue_codes": ["price_above_quote"]}]
    assert extrair_pendentes(items, "Cheney Brothers", "cotacao", _DATA) == []


def test_codigo_errado_pra_origem_nao_entra():
    # NO_QUOTE_FOR_ITEM so conta pra origem='cotacao', nao 'erp'.
    items = [{"description_invoice": "BEEF SIRLOIN", "issue_codes": ["no_quote_for_item"]}]
    assert extrair_pendentes(items, "Cheney Brothers", "erp", _DATA) == []


def test_item_sem_descricao_nao_entra():
    items = [{"description_invoice": None, "issue_codes": ["no_quote_for_item"]}]
    assert extrair_pendentes(items, "Cheney Brothers", "cotacao", _DATA) == []


def test_lista_vazia_nao_quebra():
    assert extrair_pendentes([], "Cheney Brothers", "cotacao", _DATA) == []


# ---------------------------------------------------------------------------
# sincronizar_pendentes
# ---------------------------------------------------------------------------

class _FakeSheet:
    """Substitui commons.sheets.read_values/append_values em memoria."""

    def __init__(self, existentes=None):
        self.existentes = existentes or [["FORNECEDOR", "ITEM", "ORIGEM", "DATA"]]
        self.gravados: list[list] = []
        self.checkboxes: list[tuple] = []

    def read_values(self, _sheet_id, _range, **_kw):
        return self.existentes

    def append_values(self, _sheet_id, _range, rows):
        self.gravados.extend(rows)

    def garantir_checkbox(self, _sheet_id, aba, coluna):
        self.checkboxes.append((aba, coluna))


def _instalar_fake(monkeypatch, fake: _FakeSheet):
    monkeypatch.setattr(mod, "read_values", fake.read_values)
    monkeypatch.setattr(mod, "append_values", fake.append_values)
    monkeypatch.setattr(mod, "garantir_checkbox", fake.garantir_checkbox)


def test_sheet_id_ausente_nao_faz_nada(monkeypatch):
    fake = _FakeSheet()
    _instalar_fake(monkeypatch, fake)
    n = sincronizar_pendentes(None, [PendenteRow("X", "Y", "erp", "2026-06-25")])
    assert n == 0
    assert fake.gravados == []


def test_lista_vazia_nao_faz_nada(monkeypatch):
    fake = _FakeSheet()
    _instalar_fake(monkeypatch, fake)
    assert sincronizar_pendentes("sheet-1", []) == 0
    assert fake.gravados == []


def test_pendente_nova_e_gravada(monkeypatch):
    fake = _FakeSheet()
    _instalar_fake(monkeypatch, fake)
    novas = [PendenteRow("Cheney Brothers", "BEEF SIRLOIN", "cotacao", "2026-06-25")]
    n = sincronizar_pendentes("sheet-1", novas)
    assert n == 1
    # Coluna E "Ja resolvi?" nasce com o booleano False (nunca a string).
    assert fake.gravados == [["Cheney Brothers", "BEEF SIRLOIN", "cotacao", "2026-06-25", False]]
    assert fake.gravados[0][4] is False
    assert fake.checkboxes == [("Pendentes", 4)]


def test_pendente_ja_existente_nao_duplica(monkeypatch):
    # Grafia diferente da que ja esta na aba, mas normaliza pro mesmo par.
    fake = _FakeSheet(existentes=[
        ["FORNECEDOR", "ITEM", "ORIGEM", "DATA"],
        ["cheney brothers", "beef  sirloin", "cotacao", "2026-06-01"],
    ])
    _instalar_fake(monkeypatch, fake)
    novas = [PendenteRow("Cheney Brothers", "Beef Sirloin", "cotacao", "2026-06-25")]
    n = sincronizar_pendentes("sheet-1", novas)
    assert n == 0
    assert fake.gravados == []
    assert fake.checkboxes == []


def test_pendente_marcada_como_resolvida_nao_reabre(monkeypatch):
    # Coluna E marcada (bool ou texto): o robo nao le a E, a linha segue bloqueando.
    for marca in (True, "TRUE"):
        fake = _FakeSheet(existentes=[
            ["FORNECEDOR", "ITEM", "ORIGEM", "DATA", "JA RESOLVI?"],
            ["Cheney Brothers", "BEEF SIRLOIN", "cotacao", "2026-06-01", marca],
        ])
        _instalar_fake(monkeypatch, fake)
        novas = [PendenteRow("Cheney Brothers", "BEEF SIRLOIN", "cotacao", "2026-06-25")]
        assert sincronizar_pendentes("sheet-1", novas) == 0
        assert fake.gravados == []


def test_estado_pendentes_separa_marcadas_e_abertas(monkeypatch):
    fake = _FakeSheet(existentes=[
        ["FORNECEDOR", "ITEM", "ORIGEM", "DATA", "JA RESOLVI?"],
        ["Fini", "Fini G Ovo Frito - FINI, Item #2914", "erp", "2026-10-01", True],
        ["Fini", "Chix Tender", "erp", "2026-10-01", "VERDADEIRO"],
        ["Fini", "Beef Sirloin", "erp", "2026-10-01", False],
        ["Fini", "Sassami", "erp", "2026-10-01"],          # linha curta (sem E)
        ["Fini"],                                          # sem item
    ])
    _instalar_fake(monkeypatch, fake)
    marcados, abertos = estado_pendentes("sheet-1")
    # So FALSE explicito e aberto; E marcada, vazia ou ausente (linha antiga) libera.
    assert marcados == {"FINI G OVO FRITO", "CHIX TENDER", "SASSAMI"}
    assert abertos == {"BEEF SIRLOIN"}


def test_falha_no_checkbox_nao_derruba_a_sincronizacao(monkeypatch):
    from commons.sheets import SheetsError
    fake = _FakeSheet()
    _instalar_fake(monkeypatch, fake)

    def _falha(*_a):
        raise SheetsError("quota")

    monkeypatch.setattr(mod, "garantir_checkbox", _falha)
    novas = [PendenteRow("Cheney Brothers", "BEEF SIRLOIN", "cotacao", "2026-06-25")]
    assert sincronizar_pendentes("sheet-1", novas) == 1       # linhas gravadas, so loga
    assert len(fake.gravados) == 1


def test_dedupe_usa_a_mesma_chave_do_de_para(monkeypatch):
    # Cauda de catalogo na aba nao pode fazer o mesmo item voltar.
    fake = _FakeSheet(existentes=[
        ["FORNECEDOR", "ITEM", "ORIGEM", "DATA"],
        ["Fini", "Fini G Ovo Frito - FINI, Item #2914", "erp", "2026-06-01"],
    ])
    _instalar_fake(monkeypatch, fake)
    assert sincronizar_pendentes("sheet-1", [PendenteRow("Fini", "Fini G Ovo Frito", "erp", "2026-06-25")]) == 0


def test_dedupe_dentro_do_mesmo_lote(monkeypatch):
    fake = _FakeSheet()
    _instalar_fake(monkeypatch, fake)
    novas = [
        PendenteRow("Cheney Brothers", "BEEF SIRLOIN", "cotacao", "2026-06-25"),
        PendenteRow("cheney brothers", "beef sirloin", "cotacao", "2026-06-26"),
    ]
    n = sincronizar_pendentes("sheet-1", novas)
    assert n == 1
    assert len(fake.gravados) == 1


if __name__ == "__main__":
    class _Monkeypatch:
        """Substituto minimo de pytest.monkeypatch pro modo standalone."""

        def __init__(self):
            self._orig: list[tuple] = []

        def setattr(self, obj, name, value):
            self._orig.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, value in reversed(self._orig):
                setattr(obj, name, value)

    falhas = 0
    testes = [(n, o) for n, o in sorted(globals().items())
              if n.startswith("test_") and callable(o)]
    for nome, func in testes:
        mp = _Monkeypatch()
        try:
            if "monkeypatch" in func.__code__.co_varnames[:func.__code__.co_argcount]:
                func(mp)
            else:
                func()
            print(f"  ok   {nome}")
        except AssertionError as exc:
            falhas += 1
            print(f"  FALHA {nome}: {exc or 'assert'}")
        except Exception as exc:  # noqa: BLE001
            falhas += 1
            print(f"  ERRO  {nome}: {type(exc).__name__}: {exc}")
        finally:
            mp.undo()
    print(f"\n{len(testes) - falhas}/{len(testes)} passaram")
    sys.exit(1 if falhas else 0)

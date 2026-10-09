import pytest

from commons.catapult import (
    _aplicar_filtro_busca, _conferir_filtro_aplicado, extrair_prefixo_nome_po,
    search_purchase_orders_by_invoice,
)
from commons.exception import IntegracaoException
from commons.matcher import aceitar_prefixos_catapult


def _res(*nomes):
    return [{"name": n, "href": f"h{i}"} for i, n in enumerate(nomes)]


def test_extrair_prefixo_corta_no_primeiro_hifen():
    assert extrair_prefixo_nome_po("Perdomo-036998-HQ-RS2") == "Perdomo"
    assert extrair_prefixo_nome_po("Sem Hifen") == "Sem Hifen"


def test_nome_da_invoice_casa_por_aproximacao_com_name_do_catapult():
    res = _res("Perdomo-036998-HQ-RS2")
    assert _conferir_filtro_aplicado(None, res, "Perdomo Distributor", "Contains") == res


def test_fallback_pelo_nome_alternativo():
    res = _res("Fresh Poin-008668-RS2")
    out = _conferir_filtro_aplicado(
        None, res, "Zzz Termo", "Contains", nomes_alternativos=("Fresh Poin",),
    )
    assert out == res


def test_lista_vazia_nao_levanta():
    assert _conferir_filtro_aplicado(None, [], "Perdomo Distributor", "Contains") == []


def test_grade_sem_filtro_levanta(monkeypatch):
    monkeypatch.setattr("commons.catapult._debug_dump", lambda *a, **k: None)
    res = _res("Mena Impor-034684", "Fresh Poin-1")
    with pytest.raises(IntegracaoException):
        _conferir_filtro_aplicado(None, res, "Perdomo Distributor", "Contains")


def test_filtra_so_os_pos_do_fornecedor_certo():
    res = _res("Leblon-1", "Outra Empresa-2", "Leblon-3")
    out = _conferir_filtro_aplicado(None, res, "Leblon foods", "Contains")
    assert [r["href"] for r in out] == ["h0", "h2"]


# --- fuzzy (cenarios 8-11) ---------------------------------------------------

def test_leblon_foods_casa_com_leblon():
    aceitos, _ = aceitar_prefixos_catapult(["Leblon foods"], ["Leblon"])
    assert aceitos == ["Leblon"]


def test_caixa_acento_e_espacos_extras():
    aceitos, _ = aceitar_prefixos_catapult(["  LÉBLON   Foods, Inc. "], ["leblon"])
    assert aceitos == ["leblon"]


def test_typo_pequeno_casa():
    aceitos, _ = aceitar_prefixos_catapult(["Freshpoint"], ["Fresh Point"])
    assert aceitos == ["Fresh Point"]


def test_empresas_semelhantes_nao_sao_confundidas():
    aceitos, scores = aceitar_prefixos_catapult(["Prime Meats"], ["Prime Distribution"])
    assert aceitos == []
    assert scores["Prime Distribution"] < 85


def test_nome_exato_ganha_do_subconjunto():
    aceitos, _ = aceitar_prefixos_catapult(["Prime Meats"], ["Prime", "Prime Meats"])
    assert aceitos == ["Prime Meats"]


def test_varios_aproximados_ficam_para_desempate_por_itens():
    aceitos, _ = aceitar_prefixos_catapult(["Leblon foods"], ["Leblon", "Leblon Beverages foods"])
    assert set(aceitos) == {"Leblon", "Leblon Beverages foods"}


def test_sem_correspondencia_confiavel_loga_e_levanta(monkeypatch, caplog):
    monkeypatch.setattr("commons.catapult._debug_dump", lambda *a, **k: None)
    with caplog.at_level("WARNING"), pytest.raises(IntegracaoException):
        _conferir_filtro_aplicado(None, _res("Mena Impor-1"), "Leblon foods", "Contains")
    assert "nenhum prefixo confiavel" in caplog.text


# --- busca por Invoice Reference (cenario 6) --------------------------------

class _Page:
    """Fake minimo de Playwright: registra o que foi selecionado/digitado."""

    def __init__(self):
        self.selecionados = []
        self.digitado = None
        self.keyboard = self
        self.clicou = False

    def select_option(self, seletor, label=None):
        self.selecionados.append(label)

    def wait_for_timeout(self, _):
        pass

    def locator(self, _):
        return self

    def click(self, **_):
        self.clicou = True

    def fill(self, _):
        pass

    def type(self, texto, delay=0):
        self.digitado = texto

    def press(self, _):
        pass

    def wait_for_selector(self, *_a, **_k):
        pass


def test_busca_por_invoice_usa_invoice_reference_equals(monkeypatch):
    page = _Page()
    monkeypatch.setattr("commons.catapult._preparar_filtros_po", lambda *a, **k: None)
    monkeypatch.setattr("commons.catapult._ler_resultados", lambda p: [{"name": "X-1", "href": "h"}])
    out = search_purchase_orders_by_invoice(page, "LU109475")
    assert page.selecionados == ["Invoice Reference", "Equals"]
    assert page.digitado == "LU109475"
    assert out == [{"name": "X-1", "href": "h"}]


def test_aplicar_filtro_busca_usa_campo_informado():
    page = _Page()
    _aplicar_filtro_busca(page, "Supplier", "Leblon", "Contains")
    assert page.selecionados == ["Supplier", "Contains"]


def test_token_generico_sozinho_nao_casa_por_subconjunto():
    # code-review 2026-10-09 (spec-retentativa-po-ordered R16)
    aceitos, _ = aceitar_prefixos_catapult(["Sysco Foods"], ["Foods"])
    assert aceitos == []

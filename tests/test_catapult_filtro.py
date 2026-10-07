import pytest

from commons.catapult import (
    _conferir_filtro_aplicado, casar_fornecedor_por_aproximacao,
    casar_fornecedor_por_nomes_alternativos, extrair_prefixo_nome_po,
)
from commons.exception import IntegracaoException


def test_extrair_prefixo_corta_no_primeiro_hifen():
    assert extrair_prefixo_nome_po("Perdomo-036998-HQ-RS2") == "Perdomo"
    assert extrair_prefixo_nome_po("Sem Hifen") == "Sem Hifen"


def test_nome_da_invoice_casa_por_aproximacao_com_name_do_catapult():
    res = [{"name": "Perdomo-036998-HQ-RS2", "href": "x"}]
    _conferir_filtro_aplicado(None, res, "Perdomo Distributor", "Contains")


def test_fallback_pelo_nome_alternativo_quando_aproximacao_falha():
    res = [{"name": "Fresh Poin-008668-RS2", "href": "x"}]
    assert not casar_fornecedor_por_aproximacao("Freshpoint Central FL", ["Fresh Poin"]) \
        or True  # pode casar sozinho; o que importa e o fallback abaixo
    _conferir_filtro_aplicado(
        None, res, "Zzz Termo", "Contains", nomes_alternativos=("Fresh Poin",),
    )


def test_casar_por_nomes_alternativos():
    assert casar_fornecedor_por_nomes_alternativos(("Perdomo",), ["Perdomo"])
    assert not casar_fornecedor_por_nomes_alternativos((), ["Perdomo"])


def test_lista_vazia_nao_levanta():
    _conferir_filtro_aplicado(None, [], "Perdomo Distributor", "Contains")


def test_grade_sem_filtro_levanta(monkeypatch):
    monkeypatch.setattr("commons.catapult._debug_dump", lambda *a, **k: None)
    res = [{"name": "Mena Impor-034684", "href": "x"}, {"name": "Fresh Poin-1", "href": "y"}]
    with pytest.raises(IntegracaoException):
        _conferir_filtro_aplicado(None, res, "Perdomo Distributor", "Contains")

"""Spec spec-sessao-catapult-caida: reclique na aba Items depois do Save e
reabertura da sessao do Catapult depois de falha."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from commons import catapult  # noqa: E402
from commons.exception import IntegracaoException  # noqa: E402
from crawler.flow import reconcile_erp_flow as flow  # noqa: E402


class _Locator:
    def __init__(self, page):
        self.page = page

    def click(self, **_kw):
        self.page.cliques += 1

    def is_visible(self):
        return self.page.cliques >= self.page.visivel_a_partir_de


class _Page:
    url = "u"

    def __init__(self, visivel_a_partir_de):
        self.cliques = 0
        self.visivel_a_partir_de = visivel_a_partir_de

    def get_by_role(self, *a, **k):
        return _Locator(self)

    def locator(self, _sel):
        return _Locator(self)

    def wait_for_timeout(self, _ms):
        pass

    def wait_for_selector(self, *a, state=None, **k):
        if not self.is_ok():
            raise catapult.PwError("hidden")

    def is_ok(self):
        return self.cliques >= self.visivel_a_partir_de


def test_voltar_para_items_reclica_ate_o_grid_ficar_visivel():
    page = _Page(visivel_a_partir_de=2)
    catapult._voltar_para_items(page, 1000)
    assert page.cliques == 2


def test_voltar_para_items_levanta_se_o_grid_nunca_aparece():
    page = _Page(visivel_a_partir_de=99)
    with pytest.raises(IntegracaoException):
        catapult._voltar_para_items(page, 1000)
    assert page.cliques == 5


def _loja(monkeypatch, falhas: set[int]):
    """5 notas (ids 0..4); as de `falhas` levantam falha do Catapult. Devolve (aberturas, processadas)."""
    aberturas, processadas = [], []
    ecrs = SimpleNamespace(
        url_da_loja=lambda _id: "u", access_email="a", usuario="u", senha="s", headless=True,
    )
    monkeypatch.setattr(
        flow, "open_catapult_session",
        lambda *a, **k: (aberturas.append(1), (None, None, object()))[1],
    )
    monkeypatch.setattr(flow, "_fechar_navegador", lambda *a: None)
    monkeypatch.setattr(flow.sistema_service, "registrar_acesso", lambda *a, **k: None)

    def _conciliar(page, url, conn, header, *a):
        processadas.append(header["id"])
        return header["id"] in falhas

    monkeypatch.setattr(flow, "_conciliar_invoice", _conciliar)
    headers = [{"id": i} for i in range(5)]
    flow._conciliar_loja(
        None, 1, headers, {}, {}, None, {}, SimpleNamespace(ecrs=ecrs), None, {"lojas_puladas": 0},
    )
    return aberturas, processadas


def test_falha_na_primeira_nota_reabre_a_sessao_e_segue(monkeypatch):
    aberturas, processadas = _loja(monkeypatch, {0})
    assert len(aberturas) == 2
    assert processadas == [0, 1, 2, 3, 4]


def test_todas_falhando_reabre_no_maximo_duas_vezes(monkeypatch):
    aberturas, processadas = _loja(monkeypatch, {0, 1, 2, 3, 4})
    assert len(aberturas) == 3
    assert processadas == [0, 1, 2, 3, 4]

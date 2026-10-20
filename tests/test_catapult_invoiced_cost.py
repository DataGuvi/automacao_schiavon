from decimal import Decimal

from commons.catapult import (
    _SEL_CANCEL_BUTTON, _SEL_SAVE_BUTTON, PwError, _localizar_linhas,
    atualizar_invoiced_total_cost,
)
from commons.exception import IntegracaoException
import pytest


class _Campo:
    def __init__(self, valor, desabilitado=False, falha_no_fill=False):
        self.valor, self.desabilitado, self.preenchido = valor, desabilitado, None
        self.falha_no_fill = falha_no_fill

    def locator(self, _):
        return self

    def nth(self, _):
        return self

    def is_disabled(self, timeout=None):
        return self.desabilitado

    def fill(self, texto, timeout=None):
        if self.falha_no_fill:
            raise PwError("fill estourou")
        self.preenchido = texto

    def input_value(self, timeout=None):
        return self.preenchido if self.preenchido is not None else self.valor


class _Botao:
    def __init__(self, desabilitado=False, falha=False):
        self.desabilitado, self.cliques, self.falha = desabilitado, 0, falha

    def is_disabled(self, timeout=None):
        return self.desabilitado

    def click(self, timeout=None):
        if self.falha:
            raise PwError("clique falhou")
        self.cliques += 1


class _Loc:
    def __init__(self, campos):
        self.campos = campos

    def evaluate(self, js):
        return 3

    def locator(self, _):
        return self

    def nth(self, i):
        return self.campos[i]


class _Teclado:
    def press(self, tecla):
        pass


class _Page:
    def __init__(self, campos, salvar, grade, cancelar=None):
        self.campos, self.salvar, self.grade = campos, salvar, grade
        self.cancelar = cancelar or _Botao()
        self.keyboard = _Teclado()

    def locator(self, seletor):
        if seletor == _SEL_SAVE_BUTTON:
            return self.salvar
        if seletor == _SEL_CANCEL_BUTTON:
            return self.cancelar
        return _Loc(self.campos)

    def eval_on_selector(self, seletor, js):
        return self.grade

    def wait_for_load_state(self, *a, **k):
        if getattr(self, "rede_falha", False):
            raise PwError("networkidle estourou")

    def screenshot(self, *a, **k):
        pass


def _linha(uid, custo, nome="Item", scan="1", alias="a"):
    return {"Supplier Unit ID": uid, "Scancode": scan, "Receipt Alias": alias,
            "Item Name": nome, "Invoiced Total Cost": f"${custo}"}


def _ajuste(uid, original, novo, nome="Item", scan="1", alias="a"):
    return {"po_key": 99, "valor_catapult": Decimal(original), "valor_invoice": Decimal(novo),
            "supplier_unit_id": uid, "scancode": scan, "receipt_alias": alias, "item_name": nome}


GRADE = [_linha("U1", "10.40"), _linha("U2", "5.10")]
AJUSTES = [_ajuste("U1", "10.40", "11.00"), _ajuste("U2", "5.10", "5.00")]


def test_grava_e_clica_save_uma_vez():
    campos, salvar = [_Campo("x"), _Campo("y")], _Botao()
    n = atualizar_invoiced_total_cost(_Page(campos, salvar, GRADE), AJUSTES)
    assert n == 2
    assert [c.preenchido for c in campos] == ["11.00", "5.00"]
    assert salvar.cliques == 1


def test_usa_a_posicao_atual_da_grade_e_nao_o_po_key():
    grade = [_linha("U2", "5.10"), _linha("U1", "10.40")]   # ordem trocada
    campos = [_Campo("x"), _Campo("y")]
    atualizar_invoiced_total_cost(_Page(campos, _Botao(), grade), AJUSTES)
    assert [c.preenchido for c in campos] == ["5.00", "11.00"]


def test_localizar_linhas_so_aceita_correspondencia_unica():
    assert _localizar_linhas(GRADE, AJUSTES) == [0, 1]
    assert _localizar_linhas(GRADE, [_ajuste("U1", "99.99", "11.00")]) is None   # valor mudou
    assert _localizar_linhas(GRADE, [_ajuste("U9", "10.40", "11.00")]) is None   # sumiu
    assert _localizar_linhas([_linha("U1", "10.40")] * 2, AJUSTES[:1]) is None   # duplicada
    assert _localizar_linhas(GRADE, [AJUSTES[0], AJUSTES[0]]) is None            # mesma linha 2x


def test_linha_ambigua_nao_escreve_nada():
    campos, salvar = [_Campo("x"), _Campo("y")], _Botao()
    n = atualizar_invoiced_total_cost(_Page(campos, salvar, [_linha("U1", "10.40")] * 2), AJUSTES[:1])
    assert n == 0 and all(c.preenchido is None for c in campos) and salvar.cliques == 0


def test_celula_desabilitada_nao_escreve_nada():
    campos, salvar = [_Campo("x"), _Campo("y", desabilitado=True)], _Botao()
    n = atualizar_invoiced_total_cost(_Page(campos, salvar, GRADE), AJUSTES)
    assert n == 0 and all(c.preenchido is None for c in campos) and salvar.cliques == 0


def test_save_desabilitado_nao_clica_nem_levanta():
    salvar = _Botao(desabilitado=True)
    n = atualizar_invoiced_total_cost(_Page([_Campo("x")], salvar, GRADE[:1]), AJUSTES[:1])
    assert n == 0 and salvar.cliques == 0


def test_falha_no_meio_descarta_a_edicao_com_cancel_e_levanta():
    campos, cancelar = [_Campo("x"), _Campo("y", falha_no_fill=True)], _Botao()
    with pytest.raises(IntegracaoException):
        atualizar_invoiced_total_cost(_Page(campos, _Botao(), GRADE, cancelar), AJUSTES)
    assert cancelar.cliques == 1


def test_cancel_que_falha_nao_mascara_o_erro_original():
    campos, cancelar = [_Campo("x", falha_no_fill=True)], _Botao(falha=True)
    with pytest.raises(IntegracaoException, match="fill estourou"):
        atualizar_invoiced_total_cost(_Page(campos, _Botao(), GRADE[:1], cancelar), AJUSTES[:1])


def test_save_desabilitado_descarta_a_edicao_com_cancel():
    cancelar = _Botao()
    n = atualizar_invoiced_total_cost(
        _Page([_Campo("x")], _Botao(desabilitado=True), GRADE[:1], cancelar), AJUSTES[:1])
    assert n == 0 and cancelar.cliques == 1


def test_releitura_diferente_da_celula_descarta_e_levanta():
    class _Teimoso(_Campo):
        def input_value(self, timeout=None):
            return "$1.00"
    cancelar = _Botao()
    with pytest.raises(IntegracaoException, match="mostra"):
        atualizar_invoiced_total_cost(
            _Page([_Teimoso("x")], _Botao(), GRADE[:1], cancelar), AJUSTES[:1])
    assert cancelar.cliques == 1


def test_espera_de_rede_apos_o_save_nao_vira_erro_nem_cancel():
    page = _Page([_Campo("x")], _Botao(), GRADE[:1])
    page.rede_falha = True
    assert atualizar_invoiced_total_cost(page, AJUSTES[:1]) == 1
    assert page.cancelar.cliques == 0


def test_sucesso_nao_clica_cancel():
    cancelar = _Botao()
    atualizar_invoiced_total_cost(_Page([_Campo("x")], _Botao(), GRADE[:1], cancelar), AJUSTES[:1])
    assert cancelar.cliques == 0


def test_sem_ajustes_nao_toca_na_pagina():
    assert atualizar_invoiced_total_cost(None, []) == 0

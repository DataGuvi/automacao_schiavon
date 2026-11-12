"""Spec spec-retentativa-po-ordered: retentativa de PO sem Ordered e busca por invoice."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.flow import reconcile_erp_flow as flow  # noqa: E402
from domain.enums import MAX_TENTATIVAS_PO, StatusExecucao  # noqa: E402
from domain.service import processo_service as proc  # noqa: E402
from tests.test_reconcile_erp import _item, _po  # noqa: E402


def _h(id_):
    return {"id": id_, "id_loja": 1, "cod_status": None}


# R5
def test_nota_aguardando_po_entra_fora_da_janela_sem_duplicar():
    headers = flow._selecionar_headers([], [_h(2)], [], [_h(2), _h(7)])
    assert [h["id"] for h in headers] == [2, 7]


def test_status_novo_na_faixa_de_espera():
    assert StatusExecucao.PO_NAO_ENCONTRADA == 13
    assert MAX_TENTATIVAS_PO == 3


# --- fluxo: _processar_invoice ------------------------------------------------

class _Banco:
    """Emula o contrato de `aguardar_po_ordered`: True enquanto tentativas_po < teto."""

    def __init__(self, tentativas=0):
        self.tentativas = tentativas

    def aguardar(self, conn, id_processo, etapa, max_tentativas=MAX_TENTATIVAS_PO):
        if self.tentativas == 0:
            self.tentativas = 1
            return True
        if self.tentativas < max_tentativas:
            self.tentativas += 1
            return True
        return False


def _rodar(monkeypatch, banco, achou_po):
    """Uma execucao do robo para uma nota."""
    gravou = []
    po = _po(1, "Chicken Breast", supplier_unit_id="X1", ordered="5", received="5",
             invoiced_total_cost="100")
    monkeypatch.setattr(flow, "_buscar_po", lambda *a, **k: [po] if achou_po else None)
    monkeypatch.setattr(flow.proc, "aguardar_po_ordered", banco.aguardar)
    monkeypatch.setattr(flow, "_gravar_resultado", lambda *a, **k: gravou.append(a))
    monkeypatch.setattr(flow, "ja_conciliada_erp", lambda *a, **k: False)
    totais = {"aguardando_po": 0, "sem_po": 0}
    header = {"id": 1, "id_processo": 9, "invoice_number": "N1", "supplier_name": "Acme"}
    items = [_item(1, "Chicken Breast", "20", "2.5", "50", item_code="X1")]
    flow._processar_invoice(None, "u", None, header, items, {}, lambda n: None, {}, None, totais)
    return totais, gravou


# cenario 1
def test_po_ordered_na_execucao_inicial_segue_fluxo_normal(monkeypatch):
    banco = _Banco()
    totais, gravou = _rodar(monkeypatch, banco, achou_po=True)
    assert gravou and totais["aguardando_po"] == 0 and banco.tentativas == 0


# cenarios 2-3: achada na 1a, 2a ou 3a execucao futura
@pytest.mark.parametrize("execucoes_sem_po", [1, 2, 3])
def test_po_aparece_em_execucao_futura(monkeypatch, execucoes_sem_po):
    banco = _Banco()
    for _ in range(execucoes_sem_po):
        totais, gravou = _rodar(monkeypatch, banco, achou_po=False)
        assert totais["aguardando_po"] == 1 and not gravou
    totais, gravou = _rodar(monkeypatch, banco, achou_po=True)
    assert gravou and totais["aguardando_po"] == 0


# cenarios 4-5: 1 inicial + 3 novas; a contagem passa 1 -> 2 -> 3 e encerra
def test_sem_ordered_apos_tres_novas_execucoes_reporta_sem_po(monkeypatch):
    banco = _Banco()
    contagens = []
    for _ in range(1 + MAX_TENTATIVAS_PO - 1):
        totais, gravou = _rodar(monkeypatch, banco, achou_po=False)
        contagens.append(banco.tentativas)
        assert totais["aguardando_po"] == 1 and not gravou
    assert contagens == [1, 2, 3]
    totais, gravou = _rodar(monkeypatch, banco, achou_po=False)  # 3a execucao nova
    assert gravou and totais["sem_po"] == 1 and totais["aguardando_po"] == 0


def test_lista_vazia_nao_usa_a_retentativa(monkeypatch):
    chamou = []
    monkeypatch.setattr(flow, "_buscar_po", lambda *a, **k: [])
    monkeypatch.setattr(flow.proc, "aguardar_po_ordered", lambda *a, **k: chamou.append(1) or True)
    monkeypatch.setattr(flow, "_gravar_resultado", lambda *a, **k: None)
    totais = {"aguardando_po": 0, "sem_po": 0}
    header = {"id": 1, "id_processo": 9, "invoice_number": "N1", "supplier_name": "Acme"}
    items = [_item(1, "Chicken Breast", "20", "2.5", "50", item_code="X1")]
    flow._processar_invoice(None, "u", None, header, items, {}, lambda n: None, {}, None, totais)
    assert not chamou and totais["sem_po"] == 1


# --- _buscar_po: prioridade da invoice ----------------------------------------

def _preparar_buscas(monkeypatch, por_invoice, por_nome, por_historico=()):
    chamadas = []
    po = _po(1, "Chicken Breast", supplier_unit_id="X1", ordered="5")
    monkeypatch.setattr(flow, "open_worksheets", lambda *a: None)

    def _invoice(*a, **k):
        if k.get("mostrar_historico"):
            chamadas.append("invoice_hist")
            return list(por_historico)
        chamadas.append("invoice")
        return por_invoice

    monkeypatch.setattr(flow, "search_purchase_orders_by_invoice", _invoice)
    monkeypatch.setattr(
        flow, "search_purchase_orders_by_supplier",
        lambda *a, **k: chamadas.append("nome") or por_nome,
    )
    monkeypatch.setattr(flow, "open_purchase_order", lambda *a: None)
    monkeypatch.setattr(flow, "scrape_po_items", lambda *a: [])
    monkeypatch.setattr(flow, "to_po_lines", lambda *a: [po])
    monkeypatch.setattr(flow, "escolher_po_por_itens", lambda *a: 0)
    monkeypatch.setattr(flow, "fill_receiving_invoice_info", lambda *a: None)
    monkeypatch.setattr(flow, "_aprender_depara", lambda *a, **k: None)
    return chamadas, po


HEADER = {"id": 1, "invoice_number": "LU1", "invoice_date": "2026-01-05", "supplier_name": "Leblon foods"}


# cenario 6
def test_invoice_reference_achada_nao_busca_pelo_nome(monkeypatch):
    chamadas, po = _preparar_buscas(monkeypatch, [{"href": "h"}], [{"href": "x"}])
    assert flow._buscar_po(None, "u", HEADER, [], {}, lambda n: None) == [po]
    assert chamadas == ["invoice"]


# cenario 7
def test_invoice_nao_localizada_aciona_busca_pelo_nome(monkeypatch):
    chamadas, po = _preparar_buscas(monkeypatch, [], [{"href": "h"}])
    assert flow._buscar_po(None, "u", HEADER, [], {}, lambda n: None) == [po]
    assert chamadas == ["invoice", "invoice_hist", "nome"]


def test_invoice_so_no_historico_devolve_ja_lancada_sem_buscar_nome(monkeypatch):
    chamadas, _ = _preparar_buscas(monkeypatch, [], [{"href": "h"}], por_historico=[{"href": "c"}])
    assert flow._buscar_po(None, "u", HEADER, [], {}, lambda n: None) is flow.JA_LANCADA
    assert chamadas == ["invoice", "invoice_hist"]


def test_busca_por_nome_e_uma_so_sem_historico(monkeypatch):
    chamadas, _ = _preparar_buscas(monkeypatch, [], [])
    kwargs = []
    monkeypatch.setattr(
        flow, "search_purchase_orders_by_supplier",
        lambda *a, **k: kwargs.append(k.get("mostrar_historico")) or [],
    )
    assert flow._buscar_po(None, "u", HEADER, [], {}, lambda n: None) is None
    assert kwargs == [False]


def test_ja_lancada_grava_status_final_sem_conciliar(monkeypatch):
    gravou = []
    monkeypatch.setattr(flow, "_buscar_po", lambda *a, **k: flow.JA_LANCADA)
    monkeypatch.setattr(flow.proc, "falhar_etapa", lambda *a: gravou.append(a[3]))
    monkeypatch.setattr(
        flow, "reconcile_items_against_po",
        lambda *a, **k: pytest.fail("nao deve conciliar"),
    )
    totais = {"ja_lancada": 0}
    header = {"id": 1, "id_processo": 9, "invoice_number": "N1", "supplier_name": "Acme"}
    items = [_item(1, "Chicken Breast", "20", "2.5", "50", item_code="X1")]
    flow._processar_invoice(None, "u", None, header, items, {}, lambda n: None, {}, None, totais)
    assert gravou == [StatusExecucao.INVOICE_JA_LANCADA] and totais["ja_lancada"] == 1


def test_invoice_com_varias_pos_sem_item_casando_cai_no_nome(monkeypatch):
    chamadas, po = _preparar_buscas(monkeypatch, [{"href": "a"}, {"href": "b"}], [{"href": "h"}])
    respostas = iter([None, 0])
    monkeypatch.setattr(flow, "escolher_po_por_itens", lambda *a: next(respostas))
    assert flow._buscar_po(None, "u", HEADER, [], {}, lambda n: None) == [po]
    assert chamadas == ["invoice", "nome"]


# spec-po-unica-invoice-reference
def test_invoice_com_po_unica_e_aceita_sem_item_casar(monkeypatch):
    chamadas, po = _preparar_buscas(monkeypatch, [{"href": "a"}], [{"href": "h"}])
    monkeypatch.setattr(flow, "escolher_po_por_itens", lambda *a: None)
    assert flow._buscar_po(None, "u", HEADER, [], {}, lambda n: None) == [po]
    assert chamadas == ["invoice"]


def test_busca_por_nome_com_po_unica_sem_item_casar_fica_sem_po(monkeypatch):
    _preparar_buscas(monkeypatch, [], [{"href": "h"}])
    monkeypatch.setattr(flow, "escolher_po_por_itens", lambda *a: None)
    assert flow._buscar_po(None, "u", HEADER, [], {}, lambda n: None) == []


def test_nenhuma_po_em_nenhuma_busca_devolve_none(monkeypatch):
    _preparar_buscas(monkeypatch, [], [])
    assert flow._buscar_po(None, "u", HEADER, [], {}, lambda n: None) is None


def test_sem_numero_de_invoice_vai_direto_ao_nome(monkeypatch):
    chamadas, _ = _preparar_buscas(monkeypatch, [{"href": "a"}], [{"href": "h"}])
    header = {**HEADER, "invoice_number": None}
    flow._buscar_po(None, "u", header, [], {}, lambda n: None)
    assert chamadas == ["nome"]


# spec-limpar-decimais-invoiced-total-cost
def test_decimais_excedentes_sao_limpos_antes_de_devolver_e_gravados_no_catapult(monkeypatch):
    sujo = _po(1, "Chicken Breast", supplier_unit_id="X1", ordered="5",
               invoiced_total_cost="479.9952")
    _preparar_buscas(monkeypatch, [{"href": "h"}], [])
    monkeypatch.setattr(flow, "to_po_lines", lambda *a: [sujo])
    gravados = []
    monkeypatch.setattr(
        flow, "atualizar_invoiced_total_cost",
        lambda page, ajustes: gravados.append(ajustes) or len(ajustes),
    )
    resultado = flow._buscar_po(None, "u", HEADER, [], {}, lambda n: None)
    assert resultado[0].invoiced_total_cost == Decimal("480.00")
    assert len(gravados) == 1 and gravados[0][0]["valor_invoice"] == Decimal("480.00")


# --- aguardar_po_ordered: SQL ---------------------------------------------------

class _Cur:
    def __init__(self, linha):
        self.linha, self.sql, self.params = linha, None, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.sql, self.params = sql, params

    def fetchone(self):
        return self.linha


class _Conn:
    def __init__(self, linha):
        self.cur, self.commits = _Cur(linha), 0

    def cursor(self, **k):
        return self.cur

    def commit(self):
        self.commits += 1


def test_aguardar_po_ordered_true_quando_a_update_afetou_a_linha():
    from domain.enums import Etapa
    conn = _Conn((2,))
    assert proc.aguardar_po_ordered(conn, 9, Etapa.CONCILIAR_ERP) is True
    assert int(StatusExecucao.PO_NAO_ENCONTRADA) in conn.cur.params
    assert MAX_TENTATIVAS_PO in conn.cur.params
    assert "tentativas_po" in conn.cur.sql and conn.commits == 1


def test_aguardar_po_ordered_false_quando_esgotou():
    from domain.enums import Etapa
    assert proc.aguardar_po_ordered(_Conn(None), 9, Etapa.CONCILIAR_ERP) is False


# spec-invoice-presa-em-andamento R2
def test_erro_inesperado_marca_erro_tecnico_e_nao_deixa_em_andamento(monkeypatch):
    marcadas = []

    def _explode(*a, **k):
        raise ValueError("boom")

    monkeypatch.setattr(flow, "_processar_invoice", _explode)
    monkeypatch.setattr(flow.proc, "falhar_etapa", lambda conn, id_proc, etapa, status, msg=None: marcadas.append((id_proc, status, msg)))
    totais = {"notas_erro": 0}
    header = {"id": 1, "id_processo": 9, "invoice_number": "N1", "supplier_name": "Acme"}
    flow._conciliar_invoice(None, "u", None, header, [], {}, lambda n: None, {}, None, totais)
    assert totais["notas_erro"] == 1
    assert marcadas == [(9, StatusExecucao.ERRO_NAVEGACAO, "Erro inesperado: boom")]

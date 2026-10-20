"""Testes dos helpers puros do reprocesso de INSUMO (.claude/rules/spec-reprocesso-insumo.md).

Sem rede, sem banco e sem arquivos.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from manutencao.baixar_invoices_bd import _achar, nome_original, semanas_candidatas  # noqa: E402
from manutencao.estatistica_insumo import _id_do_arquivo, referencia, resumir  # noqa: E402


def test_nome_original_remove_prefixo_de_loja_e_data_do_download():
    assert nome_original("drphil_Prime Meats_1175605__001_19-06-2026.pdf") == (
        "Prime Meats_1175605__001.pdf", date(2026, 6, 19))
    assert nome_original("wind_ZAP_06262026074322_001_26-06-2026.pdf")[0] == "ZAP_06262026074322_001.pdf"


def test_nome_original_fora_do_padrao_devolve_none():
    assert nome_original("mena_11700__001.pdf") is None
    assert nome_original("drphil_x_31-02-2026.pdf") is None


def test_semanas_candidatas_download_anterior_e_emissao():
    # 28/09/2026 e segunda; emissao em 23/09 cai na semana de 21/09 (a anterior): sem duplicar
    assert semanas_candidatas(date(2026, 9, 28), date(2026, 9, 23)) == [
        date(2026, 9, 28), date(2026, 9, 21)]
    # emissao 2010 (mal lida) nao entra
    assert semanas_candidatas(date(2026, 9, 28), date(2010, 4, 10)) == [
        date(2026, 9, 28), date(2026, 9, 21)]
    # emissao duas semanas antes entra
    assert date(2026, 9, 14) in semanas_candidatas(date(2026, 9, 28), date(2026, 9, 16))


def test_achar_por_nome_e_por_stem():
    indices = [{"a.pdf": "/x/a.pdf"}, {"b.jpg": "/x/b.jpg"}]
    assert _achar(indices, "A.PDF") == "/x/a.pdf"
    assert _achar(indices, "b.pdf") == "/x/b.jpg"
    assert _achar(indices, "c.pdf") is None


def test_id_do_arquivo():
    assert _id_do_arquivo("rep_17__drphil x.pdf") == 17


def test_referencia_insumo_categoria_ou_nao():
    assert referencia({"anotacao_geral": "Insumo Burger", "cod_status_conc": 21}) == "insumo"
    assert referencia({"anotacao_geral": None, "cod_status_conc": 21}) == "categoria"
    assert referencia({"anotacao_geral": None, "cod_status_conc": 12}) == "nao"


def _r(id_, nova, ref, erro=None):
    return {"id": id_, "arquivo": f"{id_}.pdf", "nova": nova, "ref": ref, "texto": None,
            "fornecedor": "F", "erro": erro}


def test_resumir_matriz_concordancia_e_discordancias():
    resultados = [
        _r(1, "insumo", "insumo"), _r(2, "nao", "nao"), _r(3, "nao", "categoria"),
        _r(4, "insumo", "nao"), _r(5, "nao", "insumo"), _r(6, "incerto", "insumo"),
        _r(7, None, "nao", erro="falhou"),
    ]
    resumo = resumir(resultados)
    assert resumo["lidos"] == 6
    assert resumo["concordam"] == 3
    assert [r["id"] for r in resumo["discordam"]] == [4, 5]
    assert [r["id"] for r in resumo["incertos"]] == [6]
    assert [r["id"] for r in resumo["com_erro"]] == [7]
    assert resumo["matriz"][("insumo", "insumo")] == 1

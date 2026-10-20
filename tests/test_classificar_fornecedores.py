"""spec-categoria-insumo-carne R5: casamento do script de classificacao."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from manutencao.classificar_fornecedores import ALVOS, propor, sobrando_como_insumo  # noqa: E402


def _f(id_, nome, categoria=None):
    return {"id": id_, "nome": nome, "categoria": categoria}


FORNECEDORES = [
    _f(1, "Black Bull"), _f(2, "Prime Meats"), _f(3, "Prime Distribution USA"),
    _f(4, "IDO Imports"), _f(5, "Colorado Prime"), _f(6, "Colorado Meats"), _f(7, "Zap Foods"),
]


def _candidatos(alvo):
    return dict(propor((alvo,), FORNECEDORES))[alvo]


def test_candidato_unico():
    assert [f["id"] for f in _candidatos("Black Bull")] == [1]
    assert [f["id"] for f in _candidatos("IDO")] == [4]


def test_prime_meats_nao_confunde_com_prime_distribution():
    assert [f["id"] for f in _candidatos("Prime Meats")] == [2]


def test_ambiguo_vem_com_mais_de_um_candidato():
    assert sorted(f["id"] for f in _candidatos("Colorado")) == [5, 6]


def test_nao_achado_vem_vazio():
    assert _candidatos("Kelly's") == []


def test_alvos_da_cliente():
    assert len(ALVOS["carne"]) == 8
    assert set(ALVOS["insumo"]) == {"All Florida Paper", "Rest Depot", "Sysco"}


def test_insumo_fora_dos_alvos_volta_para_outros():
    fornecedores = [_f(1, "Sysco", "insumo"), _f(2, "IDO Imports", "insumo"),
                    _f(3, "Black Bull", "carne"), _f(4, "Zap Foods", None)]
    assert [f["id"] for f in sobrando_como_insumo(fornecedores, {1})] == [2]

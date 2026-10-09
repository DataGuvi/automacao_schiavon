"""Classifica em `dim_fornecedor.categoria` os fornecedores de carne e de insumo.

Carne: a cliente lanca tudo a mao no Catapult (sem codigo de barras), e a regra de
carne (caixa x Ordered, tolerancia zero) so vale com `categoria='carne'`.
Insumo: nota desses fornecedores e pulada (nao ha pedido no Catapult), sem
depender de a Vision transcrever a palavra "insumo" (spec-categoria-insumo-carne).

Simula por padrao: so lista o que mudaria. `--aplicar` grava, e so quando o nome
casa com UM fornecedor; ambiguo ou nao achado fica na lista para decisao manual.

    python -m manutencao.classificar_fornecedores
    python -m manutencao.classificar_fornecedores --aplicar
"""

from __future__ import annotations

import argparse

from commons.db import connect_db, fechar
from commons.matcher import aceitar_prefixos_catapult
from domain.categorias import CategoriaFornecedor
from domain.config import carregar_config
from domain.service.conciliacao_service import classificar_fornecedor, fetch_fornecedores

ALVOS: dict[str, tuple[str, ...]] = {
    CategoriaFornecedor.CARNE: (
        "Black Bull", "Cheney Brothers", "Colorado", "DBA Meat",
        "Eastern", "Ernesto's Food", "Kelly's", "Prime Meats",
    ),
    CategoriaFornecedor.INSUMO: ("IDO", "Brazil USA"),
}


def propor(alvos: tuple[str, ...], fornecedores: list[dict]) -> list[tuple[str, list[dict]]]:
    """Para cada nome-alvo, os fornecedores que casam com ele (`aceitar_prefixos_catapult`:
    igual primeiro; senao nome contido no outro ou similaridade alta)."""
    por_nome: dict[str, list[dict]] = {}
    for f in fornecedores:
        por_nome.setdefault(f["nome"], []).append(f)
    resultado = []
    for alvo in alvos:
        aceitos, _scores = aceitar_prefixos_catapult([alvo], list(por_nome))
        resultado.append((alvo, [f for nome in aceitos for f in por_nome[nome]]))
    return resultado


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--aplicar", action="store_true", help="grava a categoria no banco")
    args = parser.parse_args()

    conn = connect_db(carregar_config().banco)
    try:
        fornecedores = fetch_fornecedores(conn)
        for categoria, alvos in ALVOS.items():
            print(f"\n== {categoria} ==")
            for alvo, candidatos in propor(alvos, fornecedores):
                if len(candidatos) != 1:
                    nomes = ", ".join(f"{f['id']}:{f['nome']}" for f in candidatos) or "nenhum"
                    motivo = "AMBIGUO" if candidatos else "NAO ACHADO"
                    print(f"  {motivo:<10} {alvo!r}: {nomes} -> decidir a mao")
                    continue
                f = candidatos[0]
                atual = f.get("categoria") or "-"
                if atual == categoria:
                    print(f"  ok         {alvo!r}: {f['nome']} (id {f['id']}) ja e {categoria}")
                    continue
                print(f"  {'APLICA' if args.aplicar else 'PROPOE':<10} {alvo!r}: {f['nome']} "
                      f"(id {f['id']}) {atual} -> {categoria}")
                if args.aplicar:
                    classificar_fornecedor(conn, f["id"], categoria)
    finally:
        fechar(conn)
    if not args.aplicar:
        print("\nSimulacao: nada foi gravado. Use --aplicar para gravar.")


if __name__ == "__main__":
    main()

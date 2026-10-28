"""Backfill pontual: grava `caixas` (cases) nos itens ja salvos da invoice Skyharvest
13019 (id_invoice=2871).

`qtd_caixa` foi gravado por `manutencao.atualizar_qtd_caixa_skyharvest_13019` via UPDATE
direto, sem passar pelo validator que deriva `cases` (spec-cases-derivado-qtd-caixa) -
esse validator so roda durante a leitura da Vision (`commons/vision/schema.py`), nunca
retroativo sobre linha ja persistida. Re-le o PDF original pela Vision (chama a API, tem
custo); com o `qtd_caixa` e a derivacao de `cases` ja implementados, a leitura de hoje
produz `cases` automaticamente onde a divisao e exata. Casa cada item com a linha ja
gravada por `ordem` (= `item_order`). So atualiza a coluna `caixas`; nenhuma outra muda.

    python -m manutencao.atualizar_cases_skyharvest_13019            # dry-run
    python -m manutencao.atualizar_cases_skyharvest_13019 --aplicar  # grava
"""

from __future__ import annotations

import argparse
from pathlib import Path

from commons.db import connect_db
from commons.vision import read_invoice
from domain.config import carregar_config

ARQUIVO = Path(
    r"C:\Projeto_Schivon_RPA\projeto_schiavon_agente\files\read_files"
    r"\invoice_28-10-2026\drphil_skyharvest_13019__001_25-10-2026.pdf"
)
ID_INVOICE = 2871


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aplicar", action="store_true", help="Executa o UPDATE (default: dry-run)")
    args = parser.parse_args()

    vision = carregar_config().vision
    dados = read_invoice(ARQUIVO, vision.api_key, model=vision.modelo)
    print(f"Lido: {dados.supplier_name!r} invoice {dados.invoice_number!r} - {len(dados.items)} item(ns), custo ${dados.cost_read:.4f}")

    atualizacoes = [
        (item.item_order, item.cases, item.description)
        for item in dados.items
        if item.cases is not None
    ]
    print(f"\n{len(atualizacoes)} linha(s) com cases derivado:")
    for ordem, cases, descricao in atualizacoes:
        print(f"  ordem={ordem} cases={cases} {descricao!r}")

    if not args.aplicar:
        print("\nDry-run - nada foi gravado. Rode com --aplicar para gravar.")
        return

    config = carregar_config()
    conn = connect_db(config.banco)
    try:
        with conn.cursor() as cur:
            for ordem, cases, _descricao in atualizacoes:
                cur.execute(
                    "UPDATE dwschiavon2.fat_invoice_item SET caixas = %s "
                    "WHERE id_invoice = %s AND ordem = %s",
                    (cases, ID_INVOICE, ordem),
                )
        conn.commit()
        print(f"\n{len(atualizacoes)} linha(s) atualizada(s) em fat_invoice_item (id_invoice={ID_INVOICE}).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

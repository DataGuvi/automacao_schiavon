"""Backfill pontual: grava `qtd_caixa` nos itens ja salvos da invoice Skyharvest 13019
(id_invoice=2871), lida pela Vision ANTES do campo `qtd_caixa` existir (spec-qtd-caixa).

Re-le o PDF original pela Vision (chama a API, tem custo) e casa cada item com a linha
ja gravada em `fat_invoice_item` por `ordem` (= `item_order`, mesma ordem das duas
leituras). So atualiza a coluna `qtd_caixa`; nenhuma outra coluna muda.

    python -m manutencao.atualizar_qtd_caixa_skyharvest_13019            # dry-run
    python -m manutencao.atualizar_qtd_caixa_skyharvest_13019 --aplicar  # grava
"""

from __future__ import annotations

import argparse
from pathlib import Path

from commons.db import connect_db
from commons.vision import read_invoice
from domain.config import carregar_config

ARQUIVO = Path(r"C:\Users\Cauet\Downloads\drphil_skyharvest_13019__001_25-10-2026.pdf")
ID_INVOICE = 2871


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aplicar", action="store_true", help="Executa o UPDATE (default: dry-run)")
    args = parser.parse_args()

    vision = carregar_config().vision
    dados = read_invoice(ARQUIVO, vision.api_key, model=vision.modelo)
    print(f"Lido: {dados.supplier_name!r} invoice {dados.invoice_number!r} - {len(dados.items)} item(ns), custo ${dados.cost_read:.4f}")

    atualizacoes = [
        (item.item_order, item.qtd_caixa, item.description)
        for item in dados.items
        if item.qtd_caixa is not None
    ]
    print(f"\n{len(atualizacoes)} linha(s) com qtd_caixa extraido:")
    for ordem, qtd_caixa, descricao in atualizacoes:
        print(f"  ordem={ordem} qtd_caixa={qtd_caixa} {descricao!r}")

    if not args.aplicar:
        print("\nDry-run - nada foi gravado. Rode com --aplicar para gravar.")
        return

    config = carregar_config()
    conn = connect_db(config.banco)
    try:
        with conn.cursor() as cur:
            for ordem, qtd_caixa, _descricao in atualizacoes:
                cur.execute(
                    "UPDATE dwschiavon2.fat_invoice_item SET qtd_caixa = %s "
                    "WHERE id_invoice = %s AND ordem = %s",
                    (qtd_caixa, ID_INVOICE, ordem),
                )
        conn.commit()
        print(f"\n{len(atualizacoes)} linha(s) atualizada(s) em fat_invoice_item (id_invoice={ID_INVOICE}).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

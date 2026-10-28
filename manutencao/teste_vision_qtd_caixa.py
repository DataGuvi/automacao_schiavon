"""Testa a leitura da Vision com o campo novo `qtd_caixa` (spec-qtd-caixa) em duas invoices reais.

Le cada PDF pela Vision de verdade (chama a API, tem custo) e imprime por item:
descricao, pack_size, cases, quantity, qtd_caixa. So imprime no terminal - nao grava
no banco nem move arquivo.

    python -m manutencao.teste_vision_qtd_caixa
"""

from __future__ import annotations

from pathlib import Path

from domain.config import carregar_config
from commons.vision import read_invoice

ARQUIVOS = [
    Path(r"C:\Users\Cauet\Downloads\drphil_skyharvest_13019__001_25-10-2026.pdf")
]


def imprimir_itens(arquivo: Path, api_key: str, modelo: str) -> None:
    print(f"\n=== {arquivo.name} ===")
    dados = read_invoice(arquivo, api_key, model=modelo)
    print(f"fornecedor={dados.supplier_name!r} invoice={dados.invoice_number!r} custo=${dados.cost_read:.4f}")
    for item in dados.items:
        print(
            f"  desc={item.description!r:60s} pack_size={item.pack_size!r:12s} "
            f"cases={item.cases} quantity={item.quantity} qtd_caixa={item.qtd_caixa}"
        )


def main() -> None:
    vision = carregar_config().vision
    for arquivo in ARQUIVOS:
        if not arquivo.exists():
            print(f"\n=== {arquivo.name} ===\nARQUIVO NAO ENCONTRADO: {arquivo}")
            continue
        imprimir_itens(arquivo, vision.api_key, vision.modelo)


if __name__ == "__main__":
    main()

"""Migração única: adiciona `fat_invoice_item.qtd_caixa` (NUMERIC, nullable).

Campo novo, independente de `caixas` (spec-qtd-caixa, pedido do usuário 2026-10-27):
"quantos itens/unidades tem dentro de 1 caixa/pack/display desta linha", extraído
pela Vision (`commons/vision/__init__.py` regra 9, `commons/vision/schema.py`) de
formatos comerciais claros ("12x400g", "PK 100", "10-Pk", "X12", "1 roll per box",
...). Captura e grava apenas — não participa da comparação de quantidade da
conciliação ERP (`conciliacao/reconcile_erp.py` não muda).

`IF NOT EXISTS` — rodar de novo não quebra nada.

    python -m manutencao.migrar_add_coluna_qtd_caixa            # dry-run: só mostra o SQL
    python -m manutencao.migrar_add_coluna_qtd_caixa --aplicar  # roda a migração
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from commons.db import connect_db  # noqa: E402
from domain.config import carregar_config  # noqa: E402
from domain.service.processo_service import SCHEMA  # noqa: E402

_SQL = f"""
ALTER TABLE {SCHEMA}.fat_invoice_item
    ADD COLUMN IF NOT EXISTS qtd_caixa NUMERIC;
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aplicar", action="store_true", help="Executa a migração (default: dry-run)")
    args = parser.parse_args()

    print(_SQL.strip())
    if not args.aplicar:
        print("\nDry-run — nada foi executado. Rode com --aplicar para aplicar.")
        return

    config = carregar_config()
    conn = connect_db(config.banco)
    try:
        with conn.cursor() as cur:
            cur.execute(_SQL)
        conn.commit()
        print("\nColuna 'qtd_caixa' pronta em fat_invoice_item.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

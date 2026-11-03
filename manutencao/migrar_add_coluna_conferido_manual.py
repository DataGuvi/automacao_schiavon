"""Migração única: adiciona `fat_invoice.conferido_manual_em` (TIMESTAMP, nullable).

Suporte à planilha "Conferência Manual" (spec-conferencia-manual-planilha):
marca que um humano revisou a invoice e considera "sem divergência", sem
depender do motivo original (confiança baixa, PO não encontrado, produto não
identificado) nem de `fat_conciliacao` (que pode nem existir para a nota).

`IF NOT EXISTS` — rodar de novo não quebra nada.

    python -m manutencao.migrar_add_coluna_conferido_manual            # dry-run: só mostra o SQL
    python -m manutencao.migrar_add_coluna_conferido_manual --aplicar  # roda a migração
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
ALTER TABLE {SCHEMA}.fat_invoice
    ADD COLUMN IF NOT EXISTS conferido_manual_em TIMESTAMP;
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
        print("\nColuna 'conferido_manual_em' pronta em fat_invoice.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

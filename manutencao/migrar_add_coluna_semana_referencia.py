"""Migracao unica: adiciona `fat_invoice.semana_referencia` (DATE, segunda-feira da pasta do
SharePoint de onde a nota veio) e preenche as notas ja lidas (spec-painel-semana-pasta).

Backfill: a coleta le a pasta da semana anterior e a da atual. Se a semana da emissao lida e
uma dessas duas, fica ela; senao (data lida errada), a semana anterior a leitura.

    python -m manutencao.migrar_add_coluna_semana_referencia            # dry-run: so mostra o SQL
    python -m manutencao.migrar_add_coluna_semana_referencia --aplicar  # roda a migracao
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
    ADD COLUMN IF NOT EXISTS semana_referencia DATE;

UPDATE {SCHEMA}.fat_invoice
   SET semana_referencia = CASE
         WHEN date_trunc('week', dt_emissao)::date
              BETWEEN date_trunc('week', criado_em)::date - 7 AND date_trunc('week', criado_em)::date
         THEN date_trunc('week', dt_emissao)::date
         ELSE date_trunc('week', criado_em)::date - 7
       END
 WHERE semana_referencia IS NULL;
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aplicar", action="store_true", help="Executa a migracao (default: dry-run)")
    args = parser.parse_args()

    print(_SQL.strip())
    if not args.aplicar:
        print("\nDry-run - nada foi executado. Rode com --aplicar para aplicar.")
        return

    config = carregar_config()
    conn = connect_db(config.banco)
    try:
        with conn.cursor() as cur:
            cur.execute(_SQL)
            print("\n%s nota(s) preenchida(s)." % cur.rowcount)
        conn.commit()
        print("Coluna 'semana_referencia' pronta em fat_invoice.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

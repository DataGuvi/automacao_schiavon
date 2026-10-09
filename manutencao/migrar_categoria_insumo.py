"""Migração única: aceita `insumo` em `dim_fornecedor.categoria`.

O CHECK `dim_fornecedor_categoria_chk` não conhece a categoria nova e recusa o UPDATE
(`classificar_fornecedores`, spec-categoria-insumo-carne). Recria o CHECK com todos os
valores de `domain.categorias.CategoriaFornecedor` (NULL continua valendo).

Num único ALTER (atômico). Se existir fornecedor com categoria fora da lista, o banco recusa
e nada muda. Rodar de novo não quebra: se o CHECK já aceita `insumo`, não faz nada.

    python -m manutencao.migrar_categoria_insumo            # mostra o CHECK atual e o SQL
    python -m manutencao.migrar_categoria_insumo --aplicar  # roda a migração
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from commons.db import connect_db  # noqa: E402
from domain.categorias import CategoriaFornecedor  # noqa: E402
from domain.config import carregar_config  # noqa: E402
from domain.service.processo_service import SCHEMA  # noqa: E402

_CHECK = "dim_fornecedor_categoria_chk"
_VALORES = ", ".join(f"'{c.value}'" for c in CategoriaFornecedor)

_SQL = f"""
ALTER TABLE {SCHEMA}.dim_fornecedor
    DROP CONSTRAINT {_CHECK},
    ADD CONSTRAINT {_CHECK} CHECK (categoria IN ({_VALORES}));
"""

_SQL_ATUAL = f"""
SELECT pg_get_constraintdef(oid) FROM pg_constraint
 WHERE conname = '{_CHECK}' AND conrelid = '{SCHEMA}.dim_fornecedor'::regclass
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--aplicar", action="store_true", help="Executa a migração (default: só mostra)")
    args = parser.parse_args()

    conn = connect_db(carregar_config().banco)
    try:
        with conn.cursor() as cur:
            cur.execute(_SQL_ATUAL)
            linha = cur.fetchone()
        atual = linha[0] if linha else None
        print(f"CHECK atual: {atual or '(nao existe)'}")
        if atual and "'insumo'" in atual:
            print("Ja aceita 'insumo'. Nada a fazer.")
            return

        print(f"\n{_SQL.strip()}")
        if not args.aplicar:
            print("\nNada foi executado. Rode com --aplicar para aplicar.")
            return

        with conn.cursor() as cur:
            cur.execute(_SQL if atual else _SQL.replace(f"DROP CONSTRAINT {_CHECK},\n    ", ""))
        conn.commit()
        print("\nCHECK atualizado: dim_fornecedor.categoria aceita 'insumo'.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

"""Limpeza unica: tira da planilha Conferencia Manual as invoices lidas antes do
inicio da operacao (`INICIO_OPERACAO`, 06/11/2026) - spec-conferencia-manual-corte-operacao.

    python -m manutencao.limpar_conferencia_manual_antes_operacao            # simula
    python -m manutencao.limpar_conferencia_manual_antes_operacao --aplicar  # remove
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from commons.db import connect_db, fechar  # noqa: E402
from commons.sheets import SheetsError, read_values, remover_linhas  # noqa: E402
from conciliacao.conferencia_manual import _ABA, _RANGE, _chave  # noqa: E402
from domain.config import carregar_config  # noqa: E402
from domain.service.conciliacao_service import (  # noqa: E402
    INICIO_OPERACAO, fetch_chaves_invoices_desde,
)


def linhas_a_remover(linhas: list[list], chaves_ficam: set[tuple[str, str, str]]) -> list[int]:
    """Indices (0 = cabecalho, nunca removido) das linhas de dado cuja chave nao esta em
    `chaves_ficam`. Linha vazia ou curta demais para ter chave tambem sai."""
    return [
        i for i, row in enumerate(linhas) if i > 0
        and (len(row) < 3 or _chave(row[0], row[1], row[2]) not in chaves_ficam)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--aplicar", action="store_true",
                        help="Remove da planilha (sem a flag e simulacao).")
    args = parser.parse_args()

    config = carregar_config()
    sheet_id = config.conferencia_manual_sheet_id or None
    if not sheet_id:
        raise SystemExit("CONFERENCIA_MANUAL_SHEET_ID ausente no profile")

    linhas = read_values(sheet_id, _RANGE)
    if not linhas:
        raise SystemExit(f"aba '{_ABA}' vazia (sem nem cabecalho)")

    conn = connect_db(config.banco)
    try:
        ficam = fetch_chaves_invoices_desde(conn, INICIO_OPERACAO)
    finally:
        fechar(conn)

    remover = linhas_a_remover(linhas, ficam)
    print(f"  {len(linhas) - 1} linha(s) de dado; {len(remover)} sairiam "
          f"(lidas antes de {INICIO_OPERACAO}), {len(linhas) - 1 - len(remover)} ficam")
    if not args.aplicar:
        print("\n  [simulacao] use --aplicar para remover")
        return

    try:
        remover_linhas(sheet_id, _ABA, remover)
    except SheetsError as exc:
        raise SystemExit(f"falha removendo linhas: {exc}") from exc
    print(f"\n  ok - {len(remover)} linha(s) removida(s)")


if __name__ == "__main__":
    main()

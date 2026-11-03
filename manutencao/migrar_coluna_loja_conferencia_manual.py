"""Migração única: planilha Conferência Manual ganha a coluna `Loja`.

Pedido do usuário (2026-10-30): "mude a planilha para ter a loja também e ser
possível filtrar". Layout vira `Loja | Fornecedor | Nº Invoice | Divergência
| Conferido` (era `Fornecedor | Nº Invoice | Divergência | Conferido`) —
`Loja` também passou a entrar na chave de casamento
(`conciliacao/conferencia_manual.py`, `domain/service/conciliacao_service.py
::marcar_conferido_manual`), resolvendo o limite conhecido de
`spec-conferencia-manual-planilha.md` (duas lojas com o mesmo fornecedor e
mesmo número de invoice não se confundem mais).

Passos (idempotente — já migrada, não faz nada):
  1. Insere a coluna A em branco (empurra Fornecedor/Nº Invoice/Divergência/
     Conferido para B/C/D/E), escreve o cabeçalho "Loja".
  2. Faz backfill da coluna A nas linhas já existentes, casando
     `(fornecedor, nº invoice)` contra `fat_invoice` + `dim_loja` no banco.
     Chave ambígua (mesmo fornecedor+invoice em mais de uma loja) ou sem
     casamento fica em branco e é listada no fim, para checar à mão.
  3. Refaz o checkbox `Conferido` na coluna E (era D) e liga o filtro básico
     na aba inteira.

    python -m manutencao.migrar_coluna_loja_conferencia_manual            # simula
    python -m manutencao.migrar_coluna_loja_conferencia_manual --aplicar  # grava
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from commons.db import connect_db, fechar  # noqa: E402
from commons.matcher import norm_supplier, norm_text  # noqa: E402
from commons.sheets import (  # noqa: E402
    SheetsError, garantir_checkbox, inserir_coluna, read_values,
    set_basic_filter, update_values,
)
from conciliacao.conferencia_manual import _ABA  # noqa: E402
from domain.config import carregar_config  # noqa: E402
from domain.service.processo_service import SCHEMA  # noqa: E402

_RANGE_ANTIGO = f"'{_ABA}'!A:D"


def _lojas_por_chave(conn) -> dict[tuple[str, str], list[str]]:
    """`(norm_supplier(fornecedor), norm_text(nº invoice))` -> lista de lojas
    (o banco pode ter mais de uma loja pra mesma chave — chave ambígua)."""
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT fi.nome_fornecedor, fi.numero_invoice, dl.nome
              FROM {SCHEMA}.fat_invoice fi
              JOIN {SCHEMA}.dim_loja dl ON dl.id = fi.id_loja
        """)
        linhas = cur.fetchall()
    mapa: dict[tuple[str, str], list[str]] = {}
    for fornecedor, numero_invoice, loja in linhas:
        chave = (norm_supplier(fornecedor), norm_text(str(numero_invoice)))
        mapa.setdefault(chave, [])
        if loja not in mapa[chave]:
            mapa[chave].append(loja)
    return mapa


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--aplicar", action="store_true",
                        help="Grava na planilha (sem a flag e simulacao).")
    args = parser.parse_args()

    config = carregar_config()
    sheet_id = config.conferencia_manual_sheet_id or None
    if not sheet_id:
        raise SystemExit("CONFERENCIA_MANUAL_SHEET_ID ausente no profile")

    linhas = read_values(sheet_id, _RANGE_ANTIGO)
    if not linhas:
        raise SystemExit(f"aba '{_ABA}' vazia (sem nem cabecalho)")

    cabecalho = linhas[0]
    if cabecalho and cabecalho[0].strip().upper() == "LOJA":
        print("  ja migrada (coluna A ja e 'Loja') - nada a fazer")
        return

    dados = linhas[1:]
    print(f"  {len(dados)} linha(s) de dado na aba '{_ABA}'")
    print("  1) insere a coluna 'Loja' em A (empurra as demais para a direita)")
    print(f"  2) backfill de loja em {len(dados)} linha(s), casando contra o banco")
    print("  3) checkbox 'Conferido' refeito na coluna E; filtro basico ligado")
    if not args.aplicar:
        print("\n  [simulacao] use --aplicar para gravar")
        return

    conn = connect_db(config.banco)
    try:
        mapa = _lojas_por_chave(conn)
    finally:
        fechar(conn)

    lojas: list[str] = []
    sem_casamento = ambiguas = 0
    for row in dados:
        fornecedor = row[0] if len(row) > 0 else ""
        numero_invoice = row[1] if len(row) > 1 else ""
        achadas = mapa.get((norm_supplier(fornecedor), norm_text(str(numero_invoice))), [])
        if len(achadas) == 1:
            lojas.append(achadas[0])
        elif len(achadas) > 1:
            ambiguas += 1
            lojas.append("")
            print(f"  AMBIGUO: {fornecedor!r} / {numero_invoice!r} -> {achadas}")
        else:
            sem_casamento += 1
            lojas.append("")
            print(f"  SEM CASAMENTO: {fornecedor!r} / {numero_invoice!r}")

    try:
        inserir_coluna(sheet_id, _ABA, 0)
        update_values(sheet_id, f"'{_ABA}'!A1", [["Loja"]])
        if lojas:
            update_values(sheet_id, f"'{_ABA}'!A2:A{len(lojas) + 1}", [[loja] for loja in lojas])
        garantir_checkbox(sheet_id, _ABA, 4, len(linhas))
        set_basic_filter(sheet_id, _ABA, 5)
    except SheetsError as exc:
        raise SystemExit(f"falha na migracao: {exc}") from exc

    print(f"\n  ok - {len(dados) - sem_casamento - ambiguas} linha(s) com loja preenchida, "
          f"{sem_casamento} sem casamento, {ambiguas} ambigua(s) (ficaram em branco)")


if __name__ == "__main__":
    main()

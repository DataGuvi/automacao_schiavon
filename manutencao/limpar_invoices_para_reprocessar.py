"""Apaga do banco, em cascata, os processos de invoice correspondentes aos arquivos
de uma pasta -- para reprocessar essas notas do zero (ex.: testar uma mudanca na
Vision ou na conciliacao contra dados reais ja coletados).

Mapeia cada `<arquivo>.pdf` da pasta para `processo.identificador_processo` e apaga,
na ordem certa de FK, as linhas de `fat_conciliacao_item`, `fat_conciliacao`,
`fat_invoice_item`, `fat_invoice` e `processo` daquele processo. So os arquivos que
tem processo no banco contam; o resto e ignorado com aviso. Dry-run por padrao --
so mostra quantas linhas de cada tabela seriam apagadas, por processo.

    python -m manutencao.limpar_invoices_para_reprocessar "C:\\caminho\\pasta"
    python -m manutencao.limpar_invoices_para_reprocessar "C:\\caminho\\pasta" --aplicar
"""

from __future__ import annotations

import argparse
from pathlib import Path

from commons.db import connect_db
from domain.config import carregar_config
from domain.service.processo_service import SCHEMA


def _mapear_processos(cur, pasta: Path) -> list[dict]:
    arquivos = sorted(p.name for p in pasta.glob("*.pdf"))
    if not arquivos:
        return []
    placeholders = ",".join(["%s"] * len(arquivos))
    cur.execute(
        f"SELECT id, identificador_processo FROM {SCHEMA}.processo "
        f"WHERE identificador_processo IN ({placeholders})",
        arquivos,
    )
    achados = {row[1]: row[0] for row in cur.fetchall()}
    sem_processo = [a for a in arquivos if a not in achados]
    if sem_processo:
        print(f"{len(sem_processo)} arquivo(s) sem processo no banco (ignorados):")
        for a in sem_processo:
            print(f"  {a}")
    return [{"arquivo": a, "id_processo": i} for a, i in achados.items()]


def _contar_ou_apagar(cur, ids_processo: list[int], aplicar: bool) -> None:
    placeholders = ",".join(["%s"] * len(ids_processo))

    cur.execute(f"SELECT id FROM {SCHEMA}.fat_invoice WHERE id_processo IN ({placeholders})", ids_processo)
    ids_invoice = [r[0] for r in cur.fetchall()]
    ph_invoice = ",".join(["%s"] * len(ids_invoice)) if ids_invoice else "NULL"

    cur.execute(f"SELECT id FROM {SCHEMA}.fat_conciliacao WHERE id_processo IN ({placeholders})", ids_processo)
    ids_conciliacao = [r[0] for r in cur.fetchall()]
    ph_conc = ",".join(["%s"] * len(ids_conciliacao)) if ids_conciliacao else "NULL"

    tabelas = [
        ("fat_conciliacao_item", f"id_conciliacao IN ({ph_conc})", ids_conciliacao),
        ("fat_conciliacao", f"id_processo IN ({placeholders})", ids_processo),
        ("fat_invoice_item", f"id_invoice IN ({ph_invoice})", ids_invoice),
        ("fat_invoice", f"id_processo IN ({placeholders})", ids_processo),
        ("processo", f"id IN ({placeholders})", ids_processo),
    ]

    for tabela, condicao, params in tabelas:
        if not params:
            print(f"  {tabela}: 0 linha(s)")
            continue
        if aplicar:
            cur.execute(f"DELETE FROM {SCHEMA}.{tabela} WHERE {condicao}", params)
            print(f"  {tabela}: {cur.rowcount} linha(s) apagada(s)")
        else:
            cur.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{tabela} WHERE {condicao}", params)
            print(f"  {tabela}: {cur.fetchone()[0]} linha(s)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pasta", help="Pasta com os PDFs de invoice a reprocessar")
    parser.add_argument("--aplicar", action="store_true", help="Executa o DELETE (default: dry-run)")
    args = parser.parse_args()

    pasta = Path(args.pasta)
    if not pasta.is_dir():
        raise SystemExit(f"pasta nao encontrada: {pasta}")

    config = carregar_config()
    conn = connect_db(config.banco)
    try:
        with conn.cursor() as cur:
            processos = _mapear_processos(cur, pasta)
            if not processos:
                print("Nenhum processo encontrado para os arquivos da pasta.")
                return

            print(f"\n{len(processos)} processo(s) mapeado(s):")
            for p in processos:
                print(f"  id_processo={p['id_processo']}  {p['arquivo']}")

            ids_processo = [p["id_processo"] for p in processos]
            print(f"\n{'Apagando' if args.aplicar else 'Contagem (dry-run)'}:")
            _contar_ou_apagar(cur, ids_processo, args.aplicar)

        if args.aplicar:
            conn.commit()
            print(f"\n{len(processos)} processo(s) apagado(s). Os arquivos da pasta podem ser reprocessados.")
        else:
            print("\nDry-run - nada foi apagado. Rode com --aplicar para apagar de verdade.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

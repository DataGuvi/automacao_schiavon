"""Le pela Vision e grava no banco os arquivos orfaos ja presentes em
`files/unprocessed_files` (DOWNLOAD_DIR), pulando a coleta do SharePoint.

Mesmo caminho de leitura+persistencia que `crawler/flow/invoices_flow.py` usa
(`coleta_invoices.invoice_pipeline`), so sem abrir sessao do SharePoint -- para
reprocessar manualmente arquivos que ja estao na pasta (ex.: depois de apagar os
registros antigos no banco pra testar uma mudanca na leitura).

    python -m manutencao.reler_orfaos_download_dir            # Batch API (producao)
    python -m manutencao.reler_orfaos_download_dir --sincrono  # uma chamada por arquivo
"""

from __future__ import annotations

import argparse

from commons.db import conexao
from commons.paths import DOWNLOAD_DIR, READ_DIR
from domain.config import carregar_config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sincrono", action="store_true",
                         help="Le um arquivo por vez (sem desconto de lote); default: Batch API")
    args = parser.parse_args()

    from coleta_invoices.invoice_pipeline import process_invoices_batch, process_invoices_sync

    config = carregar_config()
    if not config.vision.api_key:
        raise SystemExit("schiavon_key_vision nao configurado no profile.")

    with conexao(config.banco) as conn:
        if args.sincrono:
            process_invoices_sync(
                conn, config.vision.api_key, [], config.vision.modelo,
                DOWNLOAD_DIR, READ_DIR, config.vision.modelo_forte,
            )
        else:
            process_invoices_batch(
                conn, config.vision.api_key, [], config.vision.modelo,
                DOWNLOAD_DIR, READ_DIR, config.vision.modelo_forte,
            )


if __name__ == "__main__":
    main()

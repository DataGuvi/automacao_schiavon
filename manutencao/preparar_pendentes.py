"""Prepara a aba `Pendentes` do Google Sheets: cabecalho e checkbox da coluna E.

Coluna E "Ja resolvi?" (spec-depara-pendente). Idempotente: so escreve o
cabecalho se a E1 estiver vazia e refaz a validacao BOOLEAN da coluna inteira.

    python -m manutencao.preparar_pendentes            # simula
    python -m manutencao.preparar_pendentes --aplicar  # grava na planilha
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from commons.sheets import garantir_checkbox, read_values, update_values  # noqa: E402
from conciliacao.pendentes_sinonimo import _ABA_PENDENTES, _COLUNA_RESOLVIDO  # noqa: E402
from domain.config import carregar_config  # noqa: E402

_CABECALHO = "Já resolvi? (marque quando adicionar na aba De-Para)"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--aplicar", action="store_true",
                        help="Grava na planilha (sem a flag e simulacao).")
    args = parser.parse_args()

    sheet_id = carregar_config().sinonimos_sheet_id or None
    if not sheet_id:
        raise SystemExit("SINONIMOS_SHEET_ID ausente no profile")

    linhas = read_values(sheet_id, f"{_ABA_PENDENTES}!A1:E1")
    cab = linhas[0] if linhas else []
    tem_cabecalho = len(cab) > _COLUNA_RESOLVIDO and bool(str(cab[_COLUNA_RESOLVIDO]).strip())
    print(f"  cabecalho da E: {'ja existe' if tem_cabecalho else 'sera escrito'}")
    print("  checkbox: coluna E, da linha 2 em diante")
    if not args.aplicar:
        print("  [simulacao] use --aplicar para gravar")
        return

    if not tem_cabecalho:
        update_values(sheet_id, f"{_ABA_PENDENTES}!E1", [[_CABECALHO]])
    garantir_checkbox(sheet_id, _ABA_PENDENTES, _COLUNA_RESOLVIDO)
    print("  ok")


if __name__ == "__main__":
    main()

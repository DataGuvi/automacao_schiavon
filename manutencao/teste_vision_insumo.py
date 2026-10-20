"""Le as invoices de `files/unprocessed_files/` pela Vision e imprime no terminal o que
ela achou de manuscrito e como o robo classificaria (insumo / incerto / nao).

So le e imprime: nao grava no banco, nao toca no Catapult, nao move arquivos.
Cada leitura chama a API (custo); `--arquivo` roda um so (spec-anotacao-insumo R8).

    python -m manutencao.teste_vision_insumo
    python -m manutencao.teste_vision_insumo --arquivo "mena_11700__001.pdf"
    python -m manutencao.teste_vision_insumo --arquivo "mena_11700__001.pdf" --modelo claude-opus-5-5
"""

from __future__ import annotations

import argparse
from pathlib import Path

from commons.paths import DOWNLOAD_DIR, FILES_DIR
from commons.vision import read_invoice
from domain.service.insumo_service import classificar_anotacao_insumo, separar_itens_insumo
from domain.config import carregar_config

_EXTENSOES = {".pdf", ".jpg", ".jpeg", ".png"}


def _ler_e_imprimir(arquivo: Path, api_key: str, modelo: str) -> bool:
    """Le um arquivo e imprime o resultado. False se a leitura falhou."""
    print(f"\n=== {arquivo.name}")
    try:
        data = read_invoice(arquivo, api_key, modelo)
    except Exception as exc:  # rede de seguranca: uma nota ruim nao para a lista
        print(f"  ERRO na leitura: {type(exc).__name__}: {exc}")
        return False
    itens = [i.model_dump() for i in data.items]
    print(f"  fornecedor    : {data.supplier_name}")
    print(f"  invoice       : {data.invoice_number}")
    print(f"  confianca     : {data.reading_confidence:.0f}%  ({data.reading_status})")
    print(f"  anotacao geral: {data.general_handwritten_notes!r}")
    print(f"  obs. da IA    : {data.reading_notes!r}")
    restantes, insumo = separar_itens_insumo(itens)
    print(f"  LINHAS INSUMO ({len(insumo)}):")
    for item in insumo:
        print(f"    #{item.get('item_order')} {item.get('description')} | manuscrito: {item['handwritten_notes']!r}")
    print(f"  LINHAS SEM INSUMO ({len(restantes)}):")
    for item in restantes:
        extra = f" | manuscrito: {item['handwritten_notes']!r}" if item.get("handwritten_notes") else ""
        print(f"    #{item.get('item_order')} {item.get('description')}{extra}")
    resultado = classificar_anotacao_insumo(data.general_handwritten_notes)
    print(f"  CLASSIFICACAO : {resultado.upper()}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arquivo", help="nome de um arquivo da pasta, para ler so ele")
    parser.add_argument("--modelo", help="modelo da Vision; padrao o do profile (VISION_MODEL)")
    args = parser.parse_args()

    arquivos = sorted(
        f for f in DOWNLOAD_DIR.iterdir()
        if f.suffix.lower() in _EXTENSOES and (not args.arquivo or f.name == args.arquivo)
    )
    if args.arquivo and not arquivos:  # tambem acha o arquivo em files/conferir_insumo/**
        arquivos = sorted((FILES_DIR / "conferir_insumo").rglob(Path(args.arquivo).name))
    if not arquivos:
        print(f"Nenhum arquivo em {DOWNLOAD_DIR}")
        return

    vision = carregar_config().vision
    modelo = args.modelo or vision.modelo
    print(f"{len(arquivos)} arquivo(s) em {DOWNLOAD_DIR} - modelo {modelo}")
    falhas = sum(not _ler_e_imprimir(a, vision.api_key, modelo) for a in arquivos)
    print(f"\nFim: {len(arquivos) - falhas} lido(s), {falhas} com erro.")


if __name__ == "__main__":
    main()

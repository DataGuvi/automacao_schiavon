"""Le em lote (Batch API) os arquivos baixados por `baixar_invoices_bd` e mede a deteccao de INSUMO.

So le e imprime: nao grava no banco. Compara a classificacao nova com o que o robo decidiu antes
(concordancia, nao acerto absoluto: as discordancias sao para voce arbitrar). Retomavel: o
`batch_id` e os resultados ficam em `files/reprocesso_insumo_resultados.json`.
Spec: .claude/rules/spec-reprocesso-insumo.md.

    python -m manutencao.estatistica_insumo
    python -m manutencao.estatistica_insumo --limpar     # tira os rep_* de unprocessed_files
"""

from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from pathlib import Path

from commons.logging_config import configurar_logs, get_logger
from commons.paths import DOWNLOAD_DIR, FILES_DIR
from commons.vision import BatchItem, collect_batch_results, submit_batch, wait_for_batch
from domain.service.insumo_service import (
    INSUMO_INCERTO,
    INSUMO_NAO,
    INSUMO_SIM,
    classificar_anotacao_insumo,
)
from domain.config import carregar_config
from manutencao.baixar_invoices_bd import MANIFESTO

log = get_logger(__name__)

RESULTADOS = FILES_DIR / "reprocesso_insumo_resultados.json"
ARQUIVADOS = FILES_DIR / "reprocesso_insumo_arquivos"
_NAO_COMPARADO = 21
_LIMITE_LOTE_BYTES = 200 * 1024 * 1024  # o lote aceita 256 MB; folga para o base64
# Combinacoes (nova, referencia) que contam como concordancia. `categoria` = nota pulada pelo
# fornecedor, sem depender da Vision: a Vision ler insumo ou nao ler ali nao e erro dela.
_CONCORDA = {
    (INSUMO_SIM, "insumo"), (INSUMO_NAO, "nao"),
    (INSUMO_SIM, "categoria"), (INSUMO_NAO, "categoria"),
}


def referencia(ref: dict) -> str:
    """O que o robo decidiu antes: `insumo` (pela anotacao), `categoria` ou `nao` (spec R6)."""
    if classificar_anotacao_insumo(ref.get("anotacao_geral")) == INSUMO_SIM:
        return "insumo"
    return "categoria" if ref.get("cod_status_conc") == _NAO_COMPARADO else "nao"


def resumir(resultados: list[dict]) -> dict:
    """Matriz (nova x referencia), concordancia e listas de discordancia/incerto."""
    lidos = [r for r in resultados if not r.get("erro")]
    matriz = Counter((r["nova"], r["ref"]) for r in lidos)
    return {
        "lidos": len(lidos),
        "com_erro": [r for r in resultados if r.get("erro")],
        "matriz": matriz,
        "concordam": sum(n for par, n in matriz.items() if par in _CONCORDA),
        "incertos": [r for r in lidos if r["nova"] == INSUMO_INCERTO],
        "discordam": [r for r in lidos if r["nova"] != INSUMO_INCERTO
                      and (r["nova"], r["ref"]) not in _CONCORDA],
    }


def _linha(r: dict) -> str:
    return (f"  id={r['id']} {r['arquivo']} | fornecedor={r['fornecedor']} | "
            f"nova={r['nova']} ref={r['ref']} | texto={r['texto']!r}")


def imprimir(resumo: dict, nao_achados: list[dict], custo: float) -> None:
    lidos = resumo["lidos"]
    print(f"\n{'=' * 70}\nLidos: {lidos}  | com erro: {len(resumo['com_erro'])}  "
          f"| sem arquivo: {len(nao_achados)}  | custo: US$ {custo:.2f}")
    print("\nMatriz (linhas = leitura nova, colunas = decisao anterior):")
    colunas = ("insumo", "categoria", "nao")
    print(f"  {'':<10}" + "".join(f"{c:>11}" for c in colunas))
    for nova in (INSUMO_SIM, INSUMO_INCERTO, INSUMO_NAO):
        print(f"  {nova:<10}" + "".join(f"{resumo['matriz'][(nova, c)]:>11}" for c in colunas))
    if lidos:
        print(f"\nConcordancia: {resumo['concordam']}/{lidos} "
              f"({100 * resumo['concordam'] / lidos:.1f}%)  "
              f"| INCERTO (revisao humana): {len(resumo['incertos'])}  "
              f"| discordancias: {len(resumo['discordam'])}")
    for titulo, lista in (("DISCORDANCIAS (arbitrar)", resumo["discordam"]),
                          ("INCERTO", resumo["incertos"]), ("COM ERRO", resumo["com_erro"])):
        if lista:
            print(f"\n{titulo}:")
            for r in lista:
                print(_linha(r) if not r.get("erro") else f"  id={r['id']} {r['arquivo']}: {r['erro']}")
    if nao_achados:
        print("\nSEM ARQUIVO NO SHAREPOINT:")
        for m in nao_achados:
            print(f"  id={m['id']} {m['arquivo_bd']} ({m['motivo']})")


def _id_do_arquivo(nome: str) -> int:
    return int(nome.split("__", 1)[0].removeprefix("rep_"))


def _registro(item: BatchItem, invoice, erro: str | None, manifesto: dict) -> dict:
    m = manifesto[_id_do_arquivo(item.file_path.name)]
    base = {"id": m["id"], "arquivo": m["arquivo_bd"], "ref": referencia(m["ref"]), "erro": erro}
    if invoice is None:
        return base | {"nova": None, "texto": None, "obs": None, "fornecedor": None, "custo": 0.0}
    return base | {
        "nova": classificar_anotacao_insumo(invoice.general_handwritten_notes),
        "texto": invoice.general_handwritten_notes, "obs": invoice.reading_notes,
        "fornecedor": invoice.supplier_name, "custo": invoice.cost_read,
    }


def _ler_lote(config, manifesto: dict, poll: int) -> list[dict]:
    """Envia (ou retoma) o lote, espera e devolve um registro por arquivo."""
    vision = config.vision
    salvo = json.loads(RESULTADOS.read_text(encoding="utf-8")) if RESULTADOS.exists() else {}
    if "registros" in salvo:
        log.info("resultados ja coletados em %s", RESULTADOS)
        return salvo["registros"]

    if "batch_id" in salvo:
        batch_id = salvo["batch_id"]
        id_map = {cid: BatchItem(DOWNLOAD_DIR / nome, 0, "") for cid, nome in salvo["arquivos"].items()}
    else:
        arquivos = sorted(DOWNLOAD_DIR.glob("rep_*"))
        if sum(a.stat().st_size for a in arquivos) > _LIMITE_LOTE_BYTES:
            raise SystemExit("arquivos passam do limite de um lote; rode com menos arquivos")
        batch_id, id_map = submit_batch(
            [BatchItem(a, 0, "") for a in arquivos], vision.api_key, vision.modelo)
        RESULTADOS.write_text(json.dumps({
            "batch_id": batch_id,
            "arquivos": {cid: it.file_path.name for cid, it in id_map.items()},
        }, indent=1), encoding="utf-8")

    wait_for_batch(batch_id, vision.api_key, poll_interval=poll)
    coletados = collect_batch_results(batch_id, vision.api_key, id_map, vision.modelo)
    registros = [_registro(item, invoice, erro, manifesto) for item, invoice, erro in coletados]
    RESULTADOS.write_text(json.dumps(
        {"batch_id": batch_id, "registros": registros}, ensure_ascii=False, indent=1), encoding="utf-8")
    return registros


def _limpar() -> None:
    ARQUIVADOS.mkdir(parents=True, exist_ok=True)
    movidos = 0
    for arquivo in DOWNLOAD_DIR.glob("rep_*"):
        shutil.move(str(arquivo), ARQUIVADOS / arquivo.name)
        movidos += 1
    print(f"{movidos} arquivo(s) movido(s) para {ARQUIVADOS}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limpar", action="store_true", help="move os rep_* para fora de unprocessed_files")
    parser.add_argument("--poll", type=int, default=30, help="segundos entre consultas ao lote")
    args = parser.parse_args()
    if args.limpar:
        _limpar()
        return

    configurar_logs()
    lista = json.loads(MANIFESTO.read_text(encoding="utf-8"))
    manifesto = {m["id"]: m for m in lista}
    registros = _ler_lote(carregar_config(), manifesto, args.poll)
    nao_achados = [m for m in lista if not m["baixado"]]
    imprimir(resumir(registros), nao_achados, sum(r.get("custo") or 0 for r in registros))


if __name__ == "__main__":
    main()

"""Sincronização do de-para de vocabulário de item a partir do Google Sheets.

Os sinônimos (`CHIX→CHICKEN`, `SASSAMI→TENDER`, ...) são dado de banco
(`dwschiavon2.dim_item_sinonimo`), alimentado pela aba `De-Para` de uma
planilha do Google Sheets que o cliente edita (colunas `DE` | `PARA`).
Substitui a antiga planilha local `files/sinonimos/DE_PARA_ITENS.xlsx` — o
cliente agora edita direto no Sheets, sem depender de arquivo/execução local.

O pipeline (`main.py`) chama `sinonimos_flow(config)` como **preâmbulo**, no
início de cada execução: lê a aba `De-Para` e faz upsert idempotente. Se o
Sheets estiver indisponível (API fora do ar, credencial ausente, aba renomeada)
**pula e deixa para a próxima execução** — o preâmbulo nunca derruba o
pipeline, e a tabela segue valendo com o último estado bom.

A leitura/validação (`sinonimos_do_sheet`) não toca o banco, de propósito:
testável sem Postgres, como `matcher.py`. A validação linha a linha
(`_sinonimos_de_linhas`) é agnóstica de fonte — só itera tuplas — para poder
ser testada com uma lista fake em memória, sem precisar de rede.
"""

from __future__ import annotations

from typing import Callable, NamedTuple, Sequence

from commons.matcher import norm_item
from commons.sheets import SheetsError, read_values
from commons.logging_config import get_logger
from conciliacao.pendentes_sinonimo import estado_pendentes

log = get_logger(__name__)

_COL_DE = "DE"
_COL_PARA = "PARA"
_RANGE_DE_PARA = "De-Para!A:B"


class Sinonimo(NamedTuple):
    """Uma linha pronta para `dim_item_sinonimo`."""

    termo:      str          # como veio na planilha, limpo
    termo_norm: str          # norm_item(termo): chave de busca (token ou frase inteira)
    canonico:   str          # norm_item(PARA): termo alvo, como o Catapult escreve


class Relatorio(NamedTuple):
    """Resumo de uma leitura da aba De-Para."""

    lidas:      int
    validas:    int
    invalidas:  int
    duplicadas: int
    descartes:  list[tuple[int, str]]   # (linha, motivo) — inválidas e duplicadas
    retidas:    int = 0                 # DE de pendente ainda não marcada (rascunho)


def _clean_cell(value) -> str:
    """Texto da célula sem espaço não-separável (U+00A0) e sem espaços sobrando."""
    return str(value).replace("\xa0", " ").strip() if value is not None else ""


def _mapa_colunas(header: Sequence) -> dict[str, int]:
    """Índice das colunas `DE` e `PARA` na linha de cabeçalho, em qualquer ordem."""
    mapa: dict[str, int] = {}
    for idx, cell in enumerate(header or ()):
        rotulo = _clean_cell(cell).upper()
        if rotulo in (_COL_DE, _COL_PARA) and rotulo not in mapa:
            mapa[rotulo] = idx
    faltando = [c for c in (_COL_DE, _COL_PARA) if c not in mapa]
    if faltando:
        raise ValueError(
            f"Aba De-Para inválida: coluna(s) {', '.join(faltando)} não "
            f"encontrada(s) na primeira linha (cabeçalho esperado: 'DE' | 'PARA')."
        )
    return mapa


def _sinonimos_de_linhas(linhas: Sequence[Sequence]) -> tuple[list[Sinonimo], Relatorio]:
    """Valida linhas DE/PARA e devolve os sinônimos válidos + o relatório.

    Regras:
      * linha 1 = cabeçalho com `DE` e `PARA` (qualquer ordem);
      * linha totalmente vazia é ignorada em silêncio;
      * só um lado preenchido, ou `DE`/`PARA` que normaliza para vazio → inválida;
      * `termo_norm` repetido → a última linha vence.

    Fonte-agnóstico: recebe qualquer sequência de linhas já em memória (Sheets
    hoje). `ValueError` só quando a aba nem tem as colunas.
    """
    if not linhas:
        raise ValueError("Aba De-Para vazia (sem nem cabeçalho).")

    it = iter(linhas)
    cols = _mapa_colunas(next(it))
    i_de, i_para = cols[_COL_DE], cols[_COL_PARA]

    por_termo: dict[str, Sinonimo] = {}
    lidas = invalidas = duplicadas = 0
    descartes: list[tuple[int, str]] = []

    for n, row in enumerate(it, start=2):
        de_raw = _clean_cell(row[i_de] if len(row) > i_de else None)
        para_raw = _clean_cell(row[i_para] if len(row) > i_para else None)

        if not de_raw and not para_raw:
            continue
        lidas += 1

        if bool(de_raw) != bool(para_raw):
            invalidas += 1
            descartes.append((n, "só um lado preenchido"))
            continue

        termo_norm = norm_item(de_raw)
        canonico = norm_item(para_raw)
        if not termo_norm:
            invalidas += 1
            descartes.append((n, f"DE sem conteúdo útil: {de_raw!r}"))
            continue
        if not canonico:
            invalidas += 1
            descartes.append((n, f"PARA sem conteúdo útil: {para_raw!r}"))
            continue

        if termo_norm in por_termo:
            duplicadas += 1
            descartes.append((n, f"termo repetido, última vence: {termo_norm!r}"))
        por_termo[termo_norm] = Sinonimo(de_raw, termo_norm, canonico)

    validas = len(por_termo)
    return list(por_termo.values()), Relatorio(lidas, validas, invalidas, duplicadas, descartes)


def sinonimos_do_sheet(sheet_id: str) -> tuple[list[Sinonimo], Relatorio]:
    """Lê a aba `De-Para` da planilha e devolve os sinônimos válidos + o relatório.

    Não toca o banco. `SheetsError` propaga (API fora do ar, credencial
    ausente, planilha não compartilhada); `ValueError` quando a aba não tem as
    colunas certas.
    """
    linhas = read_values(sheet_id, _RANGE_DE_PARA)
    return _sinonimos_de_linhas(linhas)


def sincronizar(
    conn,
    sheet_id: str | None,
    *,
    _leitor: Callable[[str], tuple[list[Sinonimo], Relatorio]] = sinonimos_do_sheet,
    _estado: Callable[[str], tuple[set[str], set[str]]] = estado_pendentes,
    aplicar: bool = True,
) -> Relatorio | None:
    """Lê a aba De-Para do Sheets e faz upsert dos sinônimos liberados.

    Liberado = DE sem pendente correspondente, ou de pendente com a coluna E
    marcada; DE de pendente ainda aberta fica retido (rascunho do cliente).
    Retorna `None` quando não há planilha configurada (`sheet_id` ausente —
    feature desligada) ou quando a leitura falhou. Falha ao ler o Sheets (API
    fora do ar, credencial ausente, aba renomeada) é **pulada com aviso**,
    nunca propaga — o preâmbulo não pode derrubar o pipeline. Idempotente:
    rodar de novo com o mesmo conteúdo não muda nada.

    `_leitor`/`_estado` são injetáveis só para teste. `aplicar=False` faz dry-run.
    """
    from domain.service.conciliacao_service import (
        fetch_item_sinonimos,
        marcar_reprocesso_por_vocabulario,
        upsert_item_sinonimos,
    )

    if not sheet_id:
        return None

    try:
        linhas, relatorio = _leitor(sheet_id)
        marcados, abertos = _estado(sheet_id)
    except SheetsError as exc:
        log.warning("planilha De-Para/Pendentes indisponivel (%s); pulando nesta execucao", exc)
        return None
    except ValueError as exc:
        log.warning("planilha De-Para invalida (%s); pulando nesta execucao", exc)
        return None

    retidas = {s.termo_norm for s in linhas if s.termo_norm in abertos and s.termo_norm not in marcados}
    linhas = [s for s in linhas if s.termo_norm not in retidas]
    relatorio = relatorio._replace(retidas=len(retidas))

    mudou = False
    if aplicar:
        # O que mudou de fato: termo novo ou com destino diferente. Só isso
        # justifica reprocessar notas antigas (G10).
        atual = fetch_item_sinonimos(conn)
        mudou = any(atual.get(s.termo_norm) != s.canonico for s in linhas)
        upsert_item_sinonimos(conn, linhas)

    verbo = "sincronizados" if aplicar else "lidos (dry-run)"
    log.info(
        "%s sinonimo(s) %s (%s invalida(s), %s duplicada(s), %s retida(s) na Pendentes)",
        len(linhas), verbo, relatorio.invalidas, relatorio.duplicadas, relatorio.retidas,
    )
    for linha, motivo in relatorio.descartes:
        log.info("linha %s: %s", linha, motivo)

    if aplicar and mudou:
        n = marcar_reprocesso_por_vocabulario(conn)
        if n < 0:
            log.warning("vocabulario mudou, mas ha notas demais para remarcar automaticamente - rode a reconciliacao por periodo a mao")
        elif n:
            log.info("-> %s nota(s) remarcada(s) para reconciliacao (vocabulario mudou)", n)

    return relatorio

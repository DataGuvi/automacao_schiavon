"""Ciclo de vida do caso — a tabela `processo` do dwschiavon2.

Um caso é um arquivo de nota **ou** um ciclo de cotação. Esta é a única porta
de escrita em `processo`: os três fluxos passam por aqui, e é isso que impede
cada um de inventar sua própria regra de status e percentual.

O status nunca é escolhido à mão. Sai de `status_apos_concluir`
(`domain.enums`), que deriva da `Etapa`: a última etapa do fluxo fecha o caso
como FINALIZADO, as outras deixam EM_ANDAMENTO. O percentual sai da própria
etapa — `percentual_concluido` quando ela termina bem, `percentual_anterior`
quando falha, porque uma etapa que falhou não avança o caso.
"""

from __future__ import annotations

from datetime import date

import psycopg2
from psycopg2.extras import RealDictCursor

from commons.db import reverter
from commons.exception import DataAccessException
from domain.enums import MAX_TENTATIVAS_PO, Etapa, StatusExecucao, status_apos_concluir

# Qualificado de propósito: a migração é por fluxo, e o resto do sistema ainda
# roda no dwschiavon via search_path. Um lugar só para mudar quando terminar.
SCHEMA = "dwschiavon2"

__all__ = [
    "SCHEMA",
    "abrir",
    "concluir_etapa",
    "aguardar_etapa",
    "aguardar_po_ordered",
    "falhar_etapa",
    "registrar_contagem",
    "status_atual",
    "buscar",
]


def abrir(
    conn,
    cod_tipo: str,
    identificador_processo: str,
    id_loja: int | None = None,
    dt_origem: date | None = None,
) -> int:
    """Abre o caso, ou recupera o que já existe. Retorna o id.

    Idempotente pelo identificador do processo: rodar de novo o mesmo arquivo (ou a mesma
    semana) não cria um caso duplicado. É o que permite reprocessar sem sujar o
    banco. Não mexe em `tentativas_po`: ela é só da retentativa de PO
    (`aguardar_po_ordered`).
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {SCHEMA}.processo
                (cod_tipo, identificador_processo, id_loja, dt_origem,
                 cod_status, status_exec, percent_exec, tentativas_po)
            VALUES (%s, %s, %s, %s, %s, %s, 0, 0)
            ON CONFLICT (cod_tipo, identificador_processo) DO UPDATE
               SET atualizado_em = now() AT TIME ZONE 'America/Sao_Paulo'
            RETURNING id
            """,
            (cod_tipo, identificador_processo, id_loja, dt_origem,
             int(StatusExecucao.PENDENTE), StatusExecucao.PENDENTE.name),
        )
        id_processo = cur.fetchone()[0]
    conn.commit()
    return id_processo


def _marcar(conn, id_processo, etapa, status, percent, mensagem, custo) -> None:
    """Grava etapa, status e percentual de uma vez. O custo é acumulado."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {SCHEMA}.processo
                   SET cod_etapa     = %s,
                       etapa_exec    = %s,
                       cod_status    = %s,
                       status_exec   = %s,
                       percent_exec  = %s,
                       mensagem      = %s,
                       "custo_total_IA" = COALESCE("custo_total_IA", 0) + COALESCE(%s, 0),
                       atualizado_em = now() AT TIME ZONE 'America/Sao_Paulo'
                 WHERE id = %s
                """,
                (int(etapa), etapa.name, int(status), status.name, percent,
                 mensagem, custo, id_processo),
            )
        conn.commit()
    except psycopg2.Error as exc:
        reverter(conn)
        raise DataAccessException(
            f"falha ao gravar status do processo id={id_processo}"
        ) from exc


def concluir_etapa(
    conn,
    id_processo: int,
    etapa: Etapa,
    com_alerta: bool = False,
    custo: float | None = None,
) -> StatusExecucao:
    """Registra a etapa como concluída. Retorna o status resultante.

    `com_alerta` só tem efeito na última etapa do fluxo: fecha o caso como
    FINALIZADO_COM_ALERTA em vez de FINALIZADO, quando algo ficou para conferir.
    """
    status = status_apos_concluir(etapa, com_alerta)
    _marcar(conn, id_processo, etapa, status, etapa.percentual_concluido, None, custo)
    return status


def aguardar_etapa(
    conn,
    id_processo: int,
    etapa: Etapa,
    status: StatusExecucao = StatusExecucao.AGUARDANDO_RESPOSTA,
) -> None:
    """Marca o caso como parado à espera de algo externo.

    O percentual é o da etapa concluída: ela fez o que podia, quem falta é o
    fornecedor.
    """
    _marcar(conn, id_processo, etapa, status, etapa.percentual_concluido, None, None)


def falhar_etapa(
    conn,
    id_processo: int,
    etapa: Etapa,
    status: StatusExecucao,
    mensagem: str | None = None,
) -> None:
    """Registra a falha. O percentual fica no da etapa anterior."""
    _marcar(conn, id_processo, etapa, status, etapa.percentual_anterior,
            (mensagem or "")[:2000] or None, None)


def aguardar_po_ordered(
    conn,
    id_processo: int,
    etapa: Etapa,
    max_tentativas: int = MAX_TENTATIVAS_PO,
) -> bool:
    """Marca o caso como `PO_NAO_ENCONTRADA` e controla `tentativas_po`.

    `True` = segue aguardando (nova consulta na próxima execução do robô);
    `False` = esgotou `max_tentativas` e o caso não foi alterado — quem chama
    segue o fluxo de "nenhuma PO Ordered".

    Primeira vez (caso fora do status 13): grava 13 e `tentativas_po = 1`, mesmo
    que a coluna tenha outro valor. Já em 13 e abaixo do teto: +1. Uma única
    UPDATE condicional, então a contagem não se perde nem dobra.
    """
    status = StatusExecucao.PO_NAO_ENCONTRADA
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {SCHEMA}.processo
                   SET tentativas_po = CASE WHEN cod_status = %s
                                            THEN COALESCE(tentativas_po, 0) + 1
                                            ELSE 1 END,
                       cod_etapa     = %s,
                       etapa_exec    = %s,
                       cod_status    = %s,
                       status_exec   = %s,
                       percent_exec  = %s,
                       mensagem      = NULL,
                       atualizado_em = now() AT TIME ZONE 'America/Sao_Paulo'
                 WHERE id = %s
                   AND (cod_status IS DISTINCT FROM %s
                        OR COALESCE(tentativas_po, 0) < %s)
                RETURNING tentativas_po
                """,
                (int(status), int(etapa), etapa.name, int(status), status.name,
                 etapa.percentual_anterior, id_processo, int(status), max_tentativas),
            )
            atualizou = cur.fetchone() is not None
        conn.commit()
        return atualizou
    except psycopg2.Error as exc:
        reverter(conn)
        raise DataAccessException(
            f"falha ao gravar tentativa de PO do processo id={id_processo}"
        ) from exc


def registrar_contagem(
    conn,
    id_processo: int,
    encontrados: int | None = None,
    baixados: int | None = None,
) -> None:
    """Grava a contagem da varredura no caso 'coleta'.

    Métrica, não estado: não mexe em `cod_etapa`/`cod_status`. Passa por aqui só
    para manter a porta única de escrita em `processo`. `COALESCE` deixa cada
    lado ser gravado numa chamada separada (navegação grava `encontrados`,
    download grava `baixados`).

    `encontrados` é o retrato da última varredura; `baixados` ACUMULA: o caso
    da semana é reaberto a cada execução e cada uma só baixa o que é novo
    (spec-coleta-arquivos-soltos R13).
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {SCHEMA}.processo
               SET arquivos_encontrados = COALESCE(%s, arquivos_encontrados),
                   arquivos_baixados    = COALESCE(arquivos_baixados, 0) + COALESCE(%s, 0),
                   atualizado_em        = now() AT TIME ZONE 'America/Sao_Paulo'
             WHERE id = %s
            """,
            (encontrados, baixados, id_processo),
        )
    conn.commit()


def status_atual(conn, id_processo: int) -> StatusExecucao | None:
    """Status gravado do caso, ou None se o caso nao existe / sem status."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT cod_status FROM {SCHEMA}.processo WHERE id = %s", (id_processo,),
        )
        row = cur.fetchone()
    return StatusExecucao(row[0]) if row and row[0] is not None else None


def buscar(conn, cod_tipo: str, identificador_processo: str) -> dict | None:
    """Recupera o caso pelo identificador, ou None."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT id, cod_tipo, identificador_processo, id_loja, dt_origem,
                   cod_etapa, etapa_exec, cod_status, status_exec,
                   percent_exec, tentativas_po, mensagem, "custo_total_IA"
              FROM {SCHEMA}.processo
             WHERE cod_tipo = %s AND identificador_processo = %s
            """,
            (cod_tipo, identificador_processo),
        )
        row = cur.fetchone()
    return dict(row) if row else None

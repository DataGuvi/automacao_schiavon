"""Acesso a banco da conciliação — schema `dwschiavon2`.

De-para das tabelas:

    supplier_alias        -> dim_fornecedor_alias
    invoice_header        -> fat_invoice
    invoice_items         -> fat_invoice_item   (só tipo_linha = 'item')
    quotation_requests    -> dim_ciclo
    price_quote           -> fat_cotacao_preco
    reconciliation_header -> fat_conciliacao
    reconciliation_item   -> fat_conciliacao_item
    reconciliation_run    -> absorvida por `processo`

**A rodada deixou de existir.** No dwschiavon cada execução criava um
`reconciliation_run` e uma cópia dos resultados, então dez execuções do mesmo
período geravam dez versões e o BI precisava filtrar pela última. Agora a chave
é `(id_invoice, comparacao)`: reconciliar de novo atualiza a mesma linha. O
histórico de execução, que era o único ganho do run, mora em `processo`.

`issue_codes` (lista de texto) virou `cod_status` + `revisar`. A lista misturava
veredito de preço com sinalização de qualidade — `['handwritten_present',
'price_above_quote']` é uma divergência de preço numa nota que também tem
anotação à mão, e são coisas de naturezas diferentes. Em `fat_conciliacao`
(header) `revisar` foi removida do banco depois — só `fat_conciliacao_item`
ainda tem a coluna; no header sobra só `cod_status`/`status_conc` como veredito.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg2
from psycopg2.extras import RealDictCursor

from commons.db import reverter
from commons.exception import DataAccessException
from commons.matcher import match_supplier, norm_supplier
from domain.conciliacao_codes import MARCAS_REVISAO, IssueCode
from domain.enums import StatusConciliacao as Veredito, StatusExecucao
from domain.service.processo_service import SCHEMA

# Marca de "reconciliar de novo" — o valor é do enum, não literal solto.
_REPROC = StatusExecucao.REPROCESSAR_CONCILIACAO
_REPROC_SET = f"cod_status = {int(_REPROC)}, status_exec = '{_REPROC.name}'"

if TYPE_CHECKING:
    from conciliacao.sinonimos import Sinonimo


def _status_conciliacao(codes: list[str] | None) -> Veredito:
    """Traduz a lista de códigos no veredito único da linha.

    Ordem de precedência: não dá para comparar > diverge > confere.
    `StatusConciliacao` não reserva mais ACIMA/ABAIXO/UNIDADE_DIVERGENTE
    (eram só da comparação contra cotação, que não roda mais — ver
    `domain/enums.py`); os `IssueCode` de cotação continuam aqui só como
    rede de segurança caso `conciliacao_flow.py`/`reconcile_quote.py`
    (desativados, mas ainda no repo) rodem de novo algum dia — nesse caso
    caem no mesmo DIVERGENCIA genérico do PO, sem distinguir direção.
    """
    codes = codes or []
    if IssueCode.SKIPPED_INSUMO_ANNOTATION in codes:
        return Veredito.NAO_COMPARADO
    if (IssueCode.NO_QUOTE_FOR_ITEM in codes or IssueCode.NO_PO_FOR_ITEM in codes
            or IssueCode.PO_NAO_ENCONTRADA in codes):
        return Veredito.SEM_REFERENCIA_ITEM
    if (IssueCode.QTY_MISMATCH_PO in codes or IssueCode.PRICE_MISMATCH_PO in codes
            or IssueCode.UNIT_MISMATCH in codes or IssueCode.PRICE_ABOVE_QUOTE in codes
            or IssueCode.PRICE_BELOW_QUOTE in codes):
        return Veredito.DIVERGENCIA
    return Veredito.CONFERIDO


def _revisar(codes: list[str] | None, needs_review: bool) -> bool:
    return bool(needs_review) or bool(MARCAS_REVISAO & set(codes or []))


def _ler(conn, sql: str, params, contexto: str) -> list[dict]:
    """SELECT que devolve as linhas como `dict`. Erro do driver vira
    `DataAccessException` (com rollback), como nas gravacoes deste modulo."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    except psycopg2.Error as exc:
        reverter(conn)
        raise DataAccessException(f"falha ao ler {contexto}") from exc


# ---------------------------------------------------------------------------
# De-para de fornecedor
# ---------------------------------------------------------------------------

def fetch_supplier_aliases(conn, source: str = "invoice") -> dict[str, dict]:
    """Mapa alias_norm → {canonical_id, canonical_name, categoria}.

    A categoria vem junto porque é ela que define o escopo da conciliação. Sem
    ela o filtro era acidental: nota de papel ou bebida só ficava de fora por
    não ter alias cadastrado, e isso deixou de bastar quando o de-para passou a
    casar por aproximação.
    """
    rows = _ler(
        conn,
        f"""
        SELECT a.alias_norm,
               a.id_fornecedor AS canonical_id,
               f.nome          AS canonical_name,
               f.categoria
          FROM {SCHEMA}.dim_fornecedor_alias a
          JOIN {SCHEMA}.dim_fornecedor       f ON f.id = a.id_fornecedor
         WHERE a.origem = %s AND a.ativo
        """,
        (source,), "aliases de fornecedor",
    )
    return {r["alias_norm"]: r for r in rows}


_PISO_RESOLVER_FORNECEDOR = 90.0
_PISO_APRENDER_NOME_CATAPULT = 95.0


def fetch_fornecedores(conn) -> list[dict]:
    """Todos os fornecedores: `{id, nome, categoria, nome_catapult}`.

    `nome_catapult` e o nome como o Catapult conhece o fornecedor (prefixo do
    Name do PO): o alias `origem='erp'` ativo dele em `dim_fornecedor_alias`,
    ou `None` se ainda nao cadastrado."""
    return _ler(
        conn,
        f"""
        SELECT f.id, f.nome, f.categoria,
               (SELECT a.alias FROM {SCHEMA}.dim_fornecedor_alias a
                 WHERE a.id_fornecedor = f.id AND a.origem = 'erp' AND a.ativo
                 ORDER BY a.id LIMIT 1) AS nome_catapult
          FROM {SCHEMA}.dim_fornecedor f
        """,
        None, "fornecedores",
    )


def classificar_fornecedor(
    conn, id_fornecedor: int, categoria: str, somente_sem_categoria: bool = False,
) -> bool:
    """Grava `dim_fornecedor.categoria`. Devolve False quando nada mudou.

    `somente_sem_categoria`: so grava se a categoria atual e vazia ou 'outros' —
    o aprendizado automatico nunca sobrescreve uma classificacao feita a mao.
    Nao mexe em `cotado` (spec-categoria-insumo-carne)."""
    filtro = " AND (categoria IS NULL OR categoria = 'outros')" if somente_sem_categoria else ""
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {SCHEMA}.dim_fornecedor SET categoria = %s WHERE id = %s{filtro}",
                (str(categoria), id_fornecedor),
            )
            alterou = cur.rowcount > 0
        conn.commit()
        return alterou
    except psycopg2.Error as exc:
        reverter(conn)
        raise DataAccessException("falha ao gravar a categoria do fornecedor") from exc


def montar_resolvedor_fornecedor(fornecedores: list[dict], aliases_invoice: dict[str, dict]):
    """Devolve `resolver(nome_invoice) -> dict | None`: a linha de
    `fornecedores` da invoice. Alias `invoice` exato primeiro; senao fuzzy
    contra `dim_fornecedor.nome`; `None` quando nada casa."""
    por_id = {f["id"]: f for f in fornecedores}
    por_nome = {f["nome"]: f for f in fornecedores}

    def resolver(nome_invoice: str) -> dict | None:
        alias = aliases_invoice.get(norm_supplier(nome_invoice))
        if alias and alias["canonical_id"] in por_id:
            return por_id[alias["canonical_id"]]
        achado, _score = match_supplier(
            nome_invoice, list(por_nome), threshold=_PISO_RESOLVER_FORNECEDOR,
        )
        return por_nome.get(achado)

    return resolver


def confirmar_fornecedor_para_aprender(nome_invoice: str, fornecedor: dict) -> bool:
    """True se o nome da invoice casa a >= 95% com `dim_fornecedor.nome` do
    fornecedor resolvido. Trava o auto-aprendizado do alias 'erp': o
    fornecedor resolvido pelo piso de 90 ou por alias pode ser outro."""
    achado, _score = match_supplier(
        nome_invoice, [fornecedor["nome"]], threshold=_PISO_APRENDER_NOME_CATAPULT,
    )
    return achado is not None


def escolher_nome_catapult(nome_invoice: str, nomes_po: list[str]) -> str | None:
    """Prefixo de PO do Catapult que casa com o nome da invoice a >= 95%
    (`match_supplier`), ou `None`. E o criterio pra gravar o alias 'erp'
    sozinho."""
    achado, _score = match_supplier(
        nome_invoice, nomes_po, threshold=_PISO_APRENDER_NOME_CATAPULT,
    )
    return achado


def gravar_nome_catapult(conn, id_fornecedor: int, nome_catapult: str) -> bool:
    """Adiciona ou atualiza o alias `origem='erp'` do fornecedor.

    Atualiza o alias 'erp' que ele ja tem; senao insere. Devolve False (sem
    gravar) quando esse nome ja e alias 'erp' de OUTRO fornecedor - nunca
    reaponta cadastro alheio."""
    alias_norm = norm_supplier(nome_catapult)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT id_fornecedor FROM {SCHEMA}.dim_fornecedor_alias
                     WHERE alias_norm = %s AND origem = 'erp'""",
                (alias_norm,),
            )
            dono = cur.fetchone()
            if dono and dono[0] != id_fornecedor:
                return False
            cur.execute(
                f"""UPDATE {SCHEMA}.dim_fornecedor_alias
                       SET alias = %s, alias_norm = %s, ativo = true
                     WHERE id = (SELECT id FROM {SCHEMA}.dim_fornecedor_alias
                                  WHERE id_fornecedor = %s AND origem = 'erp' AND ativo
                                  ORDER BY id LIMIT 1)""",
                (nome_catapult, alias_norm, id_fornecedor),
            )
            if cur.rowcount == 0:
                cur.execute(
                    f"""INSERT INTO {SCHEMA}.dim_fornecedor_alias
                            (id_fornecedor, alias, alias_norm, origem)
                        VALUES (%s, %s, %s, 'erp')
                        ON CONFLICT (alias_norm, origem) DO UPDATE SET ativo = true""",
                    (id_fornecedor, nome_catapult, alias_norm),
                )
        conn.commit()
        return True
    except psycopg2.Error as exc:
        reverter(conn)
        raise DataAccessException("falha ao gravar alias erp do fornecedor") from exc


def save_supplier_alias(
    conn, canonical_id: int, canonical_name: str, alias: str,
    alias_norm: str, source: str,
) -> int:
    """Grava um apelido de fornecedor. Retorna o id."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {SCHEMA}.dim_fornecedor_alias
                (id_fornecedor, alias, alias_norm, origem)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (alias_norm, origem) DO UPDATE
               SET id_fornecedor = EXCLUDED.id_fornecedor,
                   alias         = EXCLUDED.alias,
                   ativo         = true
            RETURNING id
            """,
            (canonical_id, alias, alias_norm, source),
        )
        alias_id = cur.fetchone()[0]
    conn.commit()
    return alias_id


# ---------------------------------------------------------------------------
# De-para de vocabulário de item (sinônimos)
# ---------------------------------------------------------------------------

def fetch_item_sinonimos(conn) -> dict[str, str]:
    """Mapa termo_norm → canonico dos sinônimos de item ativos.

    Uma query por execução: o `matcher` consome isto em memória (via
    `match_items(..., sinonimos=...)`), nunca uma query por item.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT termo_norm, canonico
                  FROM {SCHEMA}.dim_item_sinonimo
                 WHERE ativo
                """
            )
            return {termo_norm: canonico for termo_norm, canonico in cur.fetchall()}
    except psycopg2.Error as exc:
        reverter(conn)
        raise DataAccessException("falha ao ler sinonimos de item") from exc


def upsert_item_sinonimos(conn, rows: list[Sinonimo]) -> int:
    """Grava/atualiza os sinônimos de item. Retorna quantas linhas foram enviadas.

    **Idempotente por `termo_norm`:** rodar de novo com a mesma planilha não
    duplica nem muda nada. `ON CONFLICT` cobre tanto inclusão quanto atualização
    (o time edita a planilha DE-PARA e o pipeline sincroniza a cada execução).
    """
    if not rows:
        return 0

    with conn.cursor() as cur:
        cur.executemany(
            f"""
            INSERT INTO {SCHEMA}.dim_item_sinonimo (termo, termo_norm, canonico)
            VALUES (%s, %s, %s)
            ON CONFLICT (termo_norm) DO UPDATE
               SET termo     = EXCLUDED.termo,
                   canonico  = EXCLUDED.canonico,
                   ativo     = true
            """,
            [(r.termo, r.termo_norm, r.canonico) for r in rows],
        )
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Insumos da conciliação
# ---------------------------------------------------------------------------

# Colunas que reconcile_one_invoice espera no header. `fat_invoice.id_fornecedor`
# não existe mais (removida do banco em produção) — o fornecedor é resolvido em
# memória a cada conciliação via `dim_fornecedor_alias` (fetch_supplier_aliases),
# não lido daqui. Qualificadas com `i.` porque a query da janela junta `processo`.
_HEADER_COLS = f"""
    i.id, i.id_processo, i.id_loja,
    i.numero_invoice          AS invoice_number,
    i.dt_emissao      AS invoice_date,
    i.nome_fornecedor AS supplier_name,
    i.total           AS total_amount,
    i.confianca       AS reading_confidence,
    i.anotacao_manual_geral AS general_handwritten_notes
"""

# cod_status que NÃO volta para a conciliação: já finalizou (com ou sem alerta)
# ou encerrou por falta de insumo. Regra: só reconcilia quem em algum momento
# errou (faixa 50-59) ou ainda não passou pela etapa. Reprocesso explícito (56)
# vem por fetch_invoice_headers_reprocesso, fora da janela.
_NAO_RECONCILIA = (
    int(StatusExecucao.FINALIZADO),
    int(StatusExecucao.FINALIZADO_COM_ALERTA),
    int(StatusExecucao.ENCERRADO_SEM_ARQUIVO),
)


def fetch_invoice_headers_for_reconciliation(
    conn, date_from, date_to, supplier: str | None = None,
) -> list[dict]:
    """Notas do período que ainda precisam conciliar.

    Só entra a nota que ainda não passou pela etapa ou que em algum momento
    errou (`processo.cod_status` fora de `_NAO_RECONCILIA`). Nota já conciliada
    sem pendência não volta só por cair na janela de data.
    """
    sql = f"""
        SELECT {_HEADER_COLS}, p.cod_status
          FROM {SCHEMA}.fat_invoice i
          JOIN {SCHEMA}.processo    p ON p.id = i.id_processo
         WHERE i.dt_emissao BETWEEN %s AND %s
           AND p.cod_status NOT IN %s
    """
    params: list = [date_from, date_to, _NAO_RECONCILIA]
    if supplier:
        sql += " AND i.nome_fornecedor ILIKE %s"
        params.append(f"%{supplier}%")
    sql += " ORDER BY i.dt_emissao, i.id"

    return _ler(conn, sql, params, "invoices a conciliar")


def ja_conciliada_erp(conn, id_invoice: int) -> bool:
    """True se a nota ja tem conciliacao ERP gravada (ja foi reportada ao cliente)."""
    rows = _ler(
        conn,
        f"""
        SELECT 1 FROM {SCHEMA}.fat_conciliacao
         WHERE id_invoice = %s AND comparacao = 'erp'
        """,
        (id_invoice,), "conciliacao ERP existente",
    )
    return bool(rows)


def fetch_invoice_headers_reprocesso(conn) -> list[dict]:
    """Notas marcadas para reconciliar de novo (processo.cod_status = 56).

    Vêm fora da janela de data: um sinônimo ou alias novo pode ter destravado
    uma nota antiga. `reconcile_quote` une esta lista à da janela.
    """
    return _ler(
        conn,
        f"""
        SELECT {_HEADER_COLS}
          FROM {SCHEMA}.fat_invoice i
         WHERE i.id_processo IN (
                   SELECT id FROM {SCHEMA}.processo
                    WHERE cod_tipo = 'invoice' AND cod_status = %s
               )
         ORDER BY i.dt_emissao, i.id
        """,
        (int(_REPROC),), "invoices marcadas para reprocesso",
    )


def fetch_invoice_headers_aguardando_po(conn) -> list[dict]:
    """Notas em `PO_NAO_ENCONTRADA` (processo.cod_status = 13), a pesquisar de novo.

    Vêm fora da janela de data: a retentativa de PO dura até 3 execuções e a
    nota não pode perder a contagem por sair das duas semanas varridas.
    """
    return _ler(
        conn,
        f"""
        SELECT {_HEADER_COLS}
          FROM {SCHEMA}.fat_invoice i
         WHERE i.id_processo IN (
                   SELECT id FROM {SCHEMA}.processo
                    WHERE cod_tipo = 'invoice' AND cod_status = %s
               )
         ORDER BY i.dt_emissao, i.id
        """,
        (int(StatusExecucao.PO_NAO_ENCONTRADA),), "invoices aguardando PO Ordered",
    )


def fetch_invoice_items_by_headers(conn, header_ids: list[int]) -> dict[int, list[dict]]:
    """Linhas de mercadoria, agrupadas por nota.

    Filtra `tipo_linha = 'item'`: frete e subtotal não têm cotação, e comparar
    um deles produz divergência falsa.
    """
    if not header_ids:
        return {}
    rows = _ler(
        conn,
        f"""
            SELECT id, id_invoice AS id_header, ordem AS item_order,
                   descricao   AS description,
                   qtd         AS quantity,
                   unidade     AS unit,
                   preco_unit  AS unit_price,
                   valor_linha AS total_price,
                   anotacao_manual AS handwritten_notes,
                   item_code, upc,
                   caixas      AS cases,
                   codigo_manual AS handwritten_code
              FROM {SCHEMA}.fat_invoice_item
             WHERE id_invoice = ANY(%s) AND tipo_linha = 'item'
             ORDER BY id_invoice, ordem NULLS LAST, id
            """,
        (header_ids,), "itens das invoices",
    )
    agrupado: dict[int, list[dict]] = {}
    for row in rows:
        agrupado.setdefault(row["id_header"], []).append(row)
    return agrupado


def fetch_request_for_date(conn, invoice_date) -> dict | None:
    """Ciclo de cotação cuja semana contém a data da nota."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT id, semana_label AS week_label,
                   inicio AS week_start, fim AS week_end
              FROM {SCHEMA}.dim_ciclo
             WHERE %s BETWEEN inicio AND fim
             ORDER BY id DESC LIMIT 1
            """,
            (invoice_date,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def fetch_quote_lines(conn, id_request: int, id_supplier: int) -> list[dict]:
    """Preços cotados por um fornecedor num ciclo. Zero significa 'não cotado'."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT id,
                   item_codigo AS item_code,
                   item_nome   AS item_name,
                   preco       AS price,
                   preco_raw   AS price_raw,
                   NULL::date  AS quote_date
              FROM {SCHEMA}.fat_cotacao_preco
             WHERE id_ciclo = %s AND id_fornecedor = %s AND preco > 0
             ORDER BY item_nome
            """,
            (id_request, id_supplier),
        )
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------

def save_reconciliation_header(conn, data: dict) -> int:
    """Grava a nota conciliada. Retorna o id.

    Sem `id_run`: a chave é (id_invoice, comparacao), então reconciliar de novo
    atualiza em vez de acumular versões. `comparacao` vem de `data` (default
    `'cotacao'`, o único valor até a frente ERP existir) — é isso que permite
    a mesma nota ter até duas linhas em `fat_conciliacao`, uma por frente.

    `id_ciclo`, `id_fornecedor`, `revisar`, `match_nivel` e `match_score` não
    existem mais nesta tabela (removidas do banco em produção). `id_fornecedor`
    também foi removida de `fat_invoice` — não tem mais FK para fornecedor em
    lugar nenhum; quem precisa dele resolve em memória via
    `fetch_supplier_aliases`/`dim_fornecedor_alias`, a cada conciliação.
    `revisar`, `match_nivel` e `match_score` sobrevivem em
    `fat_conciliacao_item`. `id_ciclo` só fazia sentido para a frente 'cotacao'
    (hoje desativada, ver módulo) e não tem substituto — se a frente 'cotacao'
    voltar, essa coluna precisa voltar.
    """
    veredito = _status_conciliacao(data.get("issue_codes"))
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {SCHEMA}.fat_conciliacao (
                    id_invoice, id_loja, id_processo,
                    comparacao, cod_status, status_conc,
                    issue_codes
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id_invoice, comparacao) DO UPDATE SET
                    id_processo   = EXCLUDED.id_processo,
                    cod_status    = EXCLUDED.cod_status,
                    status_conc   = EXCLUDED.status_conc,
                    issue_codes   = EXCLUDED.issue_codes,
                    criado_em     = now() AT TIME ZONE 'America/Sao_Paulo'
                RETURNING id
                """,
                (
                    data["id_invoice_header"], data["id_loja"], data.get("id_processo"),
                    data.get("comparacao", "cotacao"),
                    int(veredito), veredito.name,
                    data.get("issue_codes") or None,
                ),
            )
            id_conc = cur.fetchone()[0]
        conn.commit()
    except psycopg2.Error as exc:
        reverter(conn)
        raise DataAccessException(
            f"falha ao gravar conciliacao da invoice id={data['id_invoice_header']}"
        ) from exc
    return id_conc


def save_reconciliation_items(conn, id_recon_header: int, rows: list[dict]) -> int:
    """Grava as linhas comparadas. Calcula `dif_valor`, que é o que o BI soma.

    `item_referencia` aceita `item_name_quote` (frente cotação) ou
    `item_name_po` (frente ERP/Catapult, `conciliacao/reconcile_erp.py`) — as
    duas frentes gravam na mesma tabela, só `comparacao` muda (ver cabeçalho
    do módulo). `id_price_quote` não tem equivalente do lado ERP: a chave do
    PO é posicional dentro da raspagem, não um id de `fat_cotacao_preco`.

    As colunas do lado PO (`qtd_po`, `qtd_po_recebida`, `dif_qtd`,
    `valor_invoice`, `valor_po`) só vêm da frente ERP e ficam nulas na frente
    cotação — e também na linha sem par no PO, que não tem contra o que
    comparar. Linhas que casaram com o MESMO item do PO repetem esses valores:
    a comparação é feita pelo grupo somado, não linha a linha (ver
    `conciliacao/reconcile_erp.py::_comparar_grupo`).
    """
    if not rows:
        return 0

    dados = []
    for r in rows:
        veredito = _status_conciliacao(r.get("issue_codes"))
        dif, qtd = r.get("price_diff"), r.get("qty_invoice")
        dif_valor = (dif * qtd) if (dif is not None and qtd is not None) else None
        item_referencia = r.get("item_name_quote") or r.get("item_name_po")
        dados.append((
            id_recon_header, r["id_invoice_item"],
            r.get("description_invoice"), item_referencia,
            r.get("match_level"), r.get("match_score"), qtd,
            r.get("price_invoice"), r.get("price_other"),
            dif, r.get("price_diff_pct"), dif_valor,
            int(veredito), veredito.name,
            _revisar(r.get("issue_codes"), r.get("needs_review", False)),
            r.get("issue_codes") or None,
            r.get("qty_po"), r.get("qty_po_received"), r.get("qty_diff"),
            r.get("total_invoice"), r.get("total_po"),
        ))

    try:
        with conn.cursor() as cur:
            cur.executemany(
                f"""
                INSERT INTO {SCHEMA}.fat_conciliacao_item (
                    id_conciliacao, id_invoice_item,
                    descricao_invoice, item_referencia, match_nivel, match_score,
                    qtd, preco_invoice, preco_referencia,
                    dif_unitaria, dif_pct, dif_valor, cod_status, status_conc, revisar,
                    issue_codes,
                    qtd_po, qtd_po_recebida, dif_qtd, valor_invoice, valor_po
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                          %s,%s,%s,%s,%s)
                ON CONFLICT (id_conciliacao, id_invoice_item) DO UPDATE SET
                    item_referencia  = EXCLUDED.item_referencia,
                    match_nivel      = EXCLUDED.match_nivel,
                    match_score      = EXCLUDED.match_score,
                    preco_referencia = EXCLUDED.preco_referencia,
                    dif_unitaria     = EXCLUDED.dif_unitaria,
                    dif_pct          = EXCLUDED.dif_pct,
                    dif_valor        = EXCLUDED.dif_valor,
                    cod_status       = EXCLUDED.cod_status,
                    status_conc      = EXCLUDED.status_conc,
                    revisar          = EXCLUDED.revisar,
                    issue_codes      = EXCLUDED.issue_codes,
                    qtd_po           = EXCLUDED.qtd_po,
                    qtd_po_recebida  = EXCLUDED.qtd_po_recebida,
                    dif_qtd          = EXCLUDED.dif_qtd,
                    valor_invoice    = EXCLUDED.valor_invoice,
                    valor_po         = EXCLUDED.valor_po
                """,
                dados,
            )
        conn.commit()
    except psycopg2.Error as exc:
        reverter(conn)
        raise DataAccessException(
            f"falha ao gravar itens da conciliacao id={id_recon_header}"
        ) from exc
    return len(dados)


def fetch_divergencia_erp_para_relatorio(
    conn, id_invoice: int | None = None,
) -> tuple[dict, dict] | None:
    """Busca uma invoice já conciliada com divergência na frente ERP
    (`comparacao='erp'`), no formato que `conciliacao.relatorio_divergencia.
    gerar_relatorio_divergencia_erp` espera: `(header, resultado)`.

    `id_invoice`: pega essa invoice específica (ainda exige divergência
    nesta frente — `None` se ela não tiver). Sem argumento, pega a
    divergência mais recente. Uso manual — gerar um relatório de exemplo
    com dado real (`manutencao/gerar_relatorio_divergencia.py`); o fluxo
    oficial (`crawler/flow/reconcile_erp_flow.py`) já gera o relatório na
    hora, a partir do `resultado` que acabou de calcular, sem reler o banco.
    """
    sql = f"""
        SELECT fc.id AS id_conciliacao,
               fi.id, fi.numero_invoice AS invoice_number, fi.dt_emissao AS invoice_date,
               fi.nome_fornecedor AS supplier_name, fi.id_loja
          FROM {SCHEMA}.fat_conciliacao fc
          JOIN {SCHEMA}.fat_invoice fi ON fi.id = fc.id_invoice
         WHERE fc.comparacao = 'erp' AND fc.status_conc = %s
    """
    params: list = [Veredito.DIVERGENCIA.name]
    if id_invoice is not None:
        sql += " AND fc.id_invoice = %s"
        params.append(id_invoice)
    sql += " ORDER BY fc.criado_em DESC LIMIT 1"

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            if row is None:
                return None
            header = dict(row)
            id_conciliacao = header.pop("id_conciliacao")

            # Mesmas colunas das queries do Grafana ("Divergência Qtd" e
            # "Divergência Valor") — qtd/qtd_po/dif_qtd só fazem sentido pra
            # quem tem QTY_MISMATCH_PO; preco/valor/dif_unitaria, pra quem
            # tem PRICE_MISMATCH_PO. `conciliacao/relatorio_divergencia.py`
            # decide qual usar em cada seção a partir de `issue_codes`.
            cur.execute(
                f"""
                SELECT fci.descricao_invoice AS description_invoice, fci.issue_codes,
                       fii.ordem              AS item_order,
                       fci.qtd                AS qty_invoice,
                       fci.qtd_po             AS qty_po,
                       fci.dif_qtd            AS qty_diff,
                       fci.preco_invoice      AS price_invoice,
                       fci.valor_invoice      AS total_invoice,
                       fci.valor_po           AS total_po,
                       fci.dif_unitaria       AS price_diff
                  FROM {SCHEMA}.fat_conciliacao_item fci
                  JOIN {SCHEMA}.fat_invoice_item fii ON fii.id = fci.id_invoice_item
                 WHERE fci.id_conciliacao = %s
                 ORDER BY fii.ordem
                """,
                (id_conciliacao,),
            )
            itens = [dict(r) for r in cur.fetchall()]
    except psycopg2.Error as exc:
        raise DataAccessException(
            f"falha ao buscar divergencia erp para relatorio (id_invoice={id_invoice})"
        ) from exc

    return header, {"has_issue": True, "items": itens}


# ---------------------------------------------------------------------------
# Reprocesso automático (G10) — limitado ao que a correção pode destravar
# ---------------------------------------------------------------------------

# Teto de segurança: uma edição grande da planilha DE-PARA não pode reenfileirar
# a base inteira. Acima disto, o pipeline avisa e NÃO remarca — pede rodada manual.
LIMITE_REPROCESSO = 200


def _contar(conn, sql: str) -> int:
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def marcar_reprocesso_por_vocabulario(conn) -> int:
    """Remarca para reconciliar as notas que um sinônimo novo pode destravar.

    Escopo: notas já finalizadas (`cod_status` 0 ou 1) cuja conciliação tem ao
    menos uma linha `unmatched` ou `SEM_REFERENCIA_ITEM` (20). São as únicas em
    que traduzir um termo pode mudar o resultado. Retorna quantas foram
    remarcadas; -1 quando o total passa de LIMITE_REPROCESSO (nada é feito).
    """
    alvo = f"""
        SELECT p.id
          FROM {SCHEMA}.processo p
         WHERE p.cod_tipo = 'invoice' AND p.cod_status IN (0, 1)
           AND EXISTS (
               SELECT 1
                 FROM {SCHEMA}.fat_invoice fi
                 JOIN {SCHEMA}.fat_conciliacao      fc  ON fc.id_invoice = fi.id
                 JOIN {SCHEMA}.fat_conciliacao_item fci ON fci.id_conciliacao = fc.id
                WHERE fi.id_processo = p.id
                  AND (fci.match_nivel = 'unmatched' OR fci.cod_status = 20)
           )
    """
    total = _contar(conn, f"SELECT count(*) FROM ({alvo}) t")
    if total == 0:
        return 0
    if total > LIMITE_REPROCESSO:
        return -1
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {SCHEMA}.processo
               SET {_REPROC_SET},
                   atualizado_em = now() AT TIME ZONE 'America/Sao_Paulo'
             WHERE id IN ({alvo})
            """
        )
        n = cur.rowcount
    conn.commit()
    return n


def marcar_reprocesso_por_fornecedor(conn) -> int:
    """Remarca as notas que ficaram sem fornecedor (etapa 13 falhou,
    `cod_status` 55) para reconciliar de novo — chamado após aprovar aliases
    novos. Bounded por natureza: só as que já estavam nesse estado."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {SCHEMA}.processo
               SET {_REPROC_SET},
                   atualizado_em = now() AT TIME ZONE 'America/Sao_Paulo'
             WHERE cod_tipo = 'invoice' AND cod_status = {int(StatusExecucao.ERRO_SEM_FORNECEDOR)}
            """
        )
        n = cur.rowcount
    conn.commit()
    return n


def marcar_reprocesso_por_cotacao(conn) -> int:
    """Remarca as notas que fecharam com `no_quote_for_supplier` para
    reconciliar de novo — chamado depois que `check_responses` importa preço
    novo. Sem cotação sempre liga `needs_review`, então a nota fecha como
    `FINALIZADO_COM_ALERTA` (o mesmo status de qualquer outro alerta) — o
    jeito de achar só as que fecharam por falta de cotação é olhar
    `fat_conciliacao.issue_codes`, não `processo.cod_status` (não existe um
    status próprio pra isso — `ENCERRADO_SEM_COTACAO` foi removido de
    `domain/enums.py`, nunca chegou a ser emitido de verdade). Sem isto a
    nota fica presa: `FINALIZADO_COM_ALERTA` está em `_NAO_RECONCILIA`, e
    nada além disto a devolve pra janela de data.

    Vale notar: `no_quote_for_supplier` só é emitido pela comparação invoice
    x cotação (`conciliacao_flow.py`/`reconcile_quote.py`), hoje desativada
    — então esta função não encontra nada pra remarcar na prática, mas fica
    inofensiva (n=0) se `check_responses` continuar chamando."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {SCHEMA}.processo p
               SET {_REPROC_SET},
                   atualizado_em = now() AT TIME ZONE 'America/Sao_Paulo'
              FROM {SCHEMA}.fat_invoice i
              JOIN {SCHEMA}.fat_conciliacao fc ON fc.id_invoice = i.id
             WHERE p.id = i.id_processo
               AND fc.issue_codes @> ARRAY['{IssueCode.NO_QUOTE_FOR_SUPPLIER}']::text[]
            """
        )
        n = cur.rowcount
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# Relatorio Invoice x PO (Catapult) — leitura agregada, sem tocar na escrita
# acima. Nome das funcoes/abas ainda fala "Cotacao x Invoice" por historico
# (crawler/reports/painel_excel.py) — a comparacao que alimenta os dois e a
# do ERP, cotacao nao compara mais nada (ver domain/enums.py).
#
# Duas abas, duas consultas, sem interseccao (StatusConciliacao em
# domain/enums.py):
#   `fetch_comparacao_precos`  aba 1 — so o que conciliou: CONFERIDO (0)
#   `fetch_divergencias`       aba 2 — so o que precisa de acao: diverge do PO
#                              (10-19) e item sem PO pra comparar (20-29)
# ---------------------------------------------------------------------------

_DIVERGENTE = (int(Veredito.DIVERGENCIA),)
_SEM_COMPARACAO = (int(Veredito.SEM_REFERENCIA_ITEM),)


_CONCILIADO = (int(Veredito.CONFERIDO),)
_DIVERGENCIA = _DIVERGENTE + _SEM_COMPARACAO

# Colunas comuns às duas abas — o relatório é o mesmo item visto de dois
# ângulos, então nome de coluna diferente entre as abas só confundiria.
#
# `fornecedor` vem direto de `fat_invoice.nome_fornecedor` (o nome como a
# invoice foi lida) — `fat_invoice.id_fornecedor` não existe mais (removida do
# banco em produção), então não dá mais para enriquecer com o nome canônico de
# `dim_fornecedor` aqui; quem precisa do fornecedor resolvido usa
# `fetch_supplier_aliases`/`dim_fornecedor_alias` em memória, não este join.
_COLS_RELATORIO = """
                       fi.arquivo,
                       fi.nome_fornecedor                    AS fornecedor,
                       ci.descricao_invoice                  AS item,
                       ci.item_referencia                    AS item_cotado,
                       ci.qtd,
                       ci.preco_invoice, ci.preco_referencia,
                       ci.dif_unitaria, ci.dif_pct, ci.dif_valor,
                       ci.cod_status
"""

_FROM_RELATORIO = """
                  FROM {schema}.fat_conciliacao_item ci
                  JOIN {schema}.fat_conciliacao      fc ON fc.id = ci.id_conciliacao
                  JOIN {schema}.fat_invoice          fi ON fi.id = fc.id_invoice
"""


def fetch_comparacao_precos(conn, inicio, fim) -> list[dict]:
    """Aba "Cotacao x Invoice": uma linha por item que **conciliou** (preço
    cotado x preço da invoice), com o arquivo de origem.

    Só `CONFERIDO` — preço dentro da tolerância. Tudo o que não fechou (preço
    fora da tolerância, item sem par, unidade divergente) vive em
    `fetch_divergencias`, e as duas abas não se sobrepõem: quem abre a primeira
    está vendo o que deu certo, não a base inteira.

    **Preço unitário** (`preco_invoice`/`preco_referencia` de
    `fat_conciliacao_item`) — não o valor total da linha, que mistura
    quantidade com preço.
    """
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT {_COLS_RELATORIO}
                {_FROM_RELATORIO.format(schema=SCHEMA)}
                 WHERE ci.cod_status IN %s
                   AND ci.criado_em::date BETWEEN %s AND %s
                 ORDER BY fi.arquivo, ci.descricao_invoice
                """,
                (_CONCILIADO, inicio, fim),
            )
            return [dict(r) for r in cur.fetchall()]
    except psycopg2.Error as exc:
        raise DataAccessException(
            "painel - falha ao consultar comparacao de precos"
        ) from exc


def fetch_divergencias(conn, inicio, fim) -> list[dict]:
    """Aba "Divergencias Cotacao x Invoice": só o item que não fechou.

    Junta as duas naturezas de problema numa lista de trabalho única, porque
    para quem confere a nota as duas terminam no mesmo lugar — ligar para o
    fornecedor ou corrigir o de-para:

        10-19  preço fora da tolerância  -> tem os dois preços e a diferença
        20-29  sem comparação            -> `preco_referencia` vem nulo

    Ordena pelo maior impacto em valor (`dif_valor`), com os sem comparação no
    fim: quem abre a aba vê primeiro o que custa dinheiro.
    """
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT {_COLS_RELATORIO}
                {_FROM_RELATORIO.format(schema=SCHEMA)}
                 WHERE ci.cod_status IN %s
                   AND ci.criado_em::date BETWEEN %s AND %s
                 ORDER BY ABS(COALESCE(ci.dif_valor, 0)) DESC,
                          fi.arquivo, ci.descricao_invoice
                """,
                (_DIVERGENCIA, inicio, fim),
            )
            return [dict(r) for r in cur.fetchall()]
    except psycopg2.Error as exc:
        raise DataAccessException(
            "painel - falha ao consultar divergencias cotacao x invoice"
        ) from exc

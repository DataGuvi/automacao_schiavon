"""Acesso a banco da coleta de invoices — schema `dwschiavon2`.

De-para das tabelas:

    configs         -> dim_loja + dim_fonte
    execution_log   -> processo, tipo 'coleta'
    invoice_header  -> fat_invoice   (+ processo, tipo 'invoice')
    invoice_items   -> fat_invoice_item

Dois casos distintos, de propósito:

    tipo 'coleta'   uma varredura do SharePoint, por loja e semana.
                    Existe mesmo quando não acha arquivo nenhum — e é por isso
                    que ela é um caso próprio: no dwschiavon, 47 das 148 linhas
                    de execution_log eram "pasta não encontrada", e uma
                    navegação sem arquivo não teria onde morar se a coleta
                    fosse apenas uma etapa da nota.

    tipo 'invoice'  um arquivo baixado, lido e conciliado.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from psycopg2.extras import RealDictCursor

from commons.texto import truncar
from domain.classificacao import tipo_linha
from domain.enums import Etapa, StatusExecucao as Status
from domain.service import processo_service as proc
from domain.service.processo_service import SCHEMA

if TYPE_CHECKING:
    # `.vision` nao existe em domain/service/ — o import so nao estourava
    # porque TYPE_CHECKING e False em runtime. O schema mora ao lado do
    # cliente da Vision.
    from commons.vision.schema import InvoiceData

# Abaixo disto a leitura da nota fica marcada para conferência humana (`revisar`),
# mas NÃO trava: a nota segue para a conciliação normalmente. Mesmo valor do
# MIN_READING_CONFIDENCE do reconcile_quote.py por coincidência de piso, não de
# proposito — la o sentido e outro: "confiavel o bastante para ACUSAR erro de
# preco". Os dois pisos continuam variaveis separadas de proposito (podem
# divergir de novo no futuro).
CONFIANCA_MINIMA_PAINEL = 70

# Tolerância da conferência de soma da nota (G9): fecha quando
# |total - soma(item+encargo)| não passa de 1% do total (piso de 5 centavos).
_TOLERANCIA_SOMA_PCT = 0.01
_TOLERANCIA_SOMA_MIN = 0.05


# ---------------------------------------------------------------------------
# Fontes de coleta
# ---------------------------------------------------------------------------

def fetch_configs_by_tool(conn, tool: str) -> list[dict]:
    """Fontes ativas de uma ferramenta, uma por loja.

    Sai com as chaves antigas (`id`, `name`, `tool`, `url`) para o orquestrador
    não mudar. `id` é o da **loja** — os ids foram preservados na migração, então
    Windermere continua 1 e Dr. Phillips continua 2.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT l.id,
                   l.nome        AS name,
                   l.codigo      AS codigo_loja,
                   f.ferramenta  AS tool,
                   f.url,
                   l.nome        AS description
              FROM {SCHEMA}.dim_fonte f
              JOIN {SCHEMA}.dim_loja  l ON l.id = f.id_loja
             WHERE f.ferramenta = %s AND f.ativo AND l.ativo
             ORDER BY l.id
            """,
            (tool,),
        )
        return [dict(row) for row in cur.fetchall()]


def fetch_all_configs(conn) -> list[dict]:
    """Fontes de SharePoint, que é de onde as invoices vêm."""
    return fetch_configs_by_tool(conn, "sharepoint")


# ---------------------------------------------------------------------------
# Caso 'coleta' — a varredura
# ---------------------------------------------------------------------------

def abrir_coleta(conn, id_loja: int, referencia: date) -> int:
    """Abre (ou recupera) o caso da varredura desta loja nesta semana."""
    return proc.abrir(
        conn,
        cod_tipo="coleta",
        identificador_processo=f"loja{id_loja}:{referencia:%Y-%m-%d}",
        id_loja=id_loja,
        dt_origem=referencia,
    )


# Desfechos de uma varredura que ja baixou: nao sao rebaixados por uma
# varredura posterior sem arquivo solto (R14).
_COLETA_FINALIZADA = (Status.FINALIZADO, Status.FINALIZADO_COM_ALERTA)


def registrar_navegacao(
    conn, id_coleta: int, caminho: str | None, arquivos: int,
) -> None:
    """Fecha a etapa NAVEGAR.

    Pasta encontrada e vazia não é erro — é ENCERRADO_SEM_ARQUIVO. Tratar as
    duas coisas como falha era o que fazia a semana sem entrega parecer bug.

    O caso é da semana e reaberto a cada execução: semana já FINALIZADA não
    volta para "sem arquivo" quando o cliente move as notas para LANÇADAS
    (spec-coleta-arquivos-soltos R14).
    """
    if arquivos:
        proc.concluir_etapa(conn, id_coleta, Etapa.NAVEGAR)
    elif proc.status_atual(conn, id_coleta) not in _COLETA_FINALIZADA:
        proc.falhar_etapa(
            conn, id_coleta, Etapa.NAVEGAR, Status.ENCERRADO_SEM_ARQUIVO,
            f"Pasta '{caminho}' sem arquivos.",
        )
    # Contagem para o painel — grava 0 inclusive no ramo sem arquivo.
    proc.registrar_contagem(conn, id_coleta, encontrados=arquivos)


def falhar_navegacao(conn, id_coleta: int, mensagem: str) -> None:
    """A pasta da semana não foi encontrada — o caso mais comum de falha."""
    proc.falhar_etapa(
        conn, id_coleta, Etapa.NAVEGAR, Status.ERRO_NAVEGACAO, mensagem,
    )


def falhar_login(conn, id_coleta: int, mensagem: str) -> None:
    """Falha de autenticação no SharePoint — distinta de navegação, para o
    painel e o alerta dizerem que caiu o LOGIN, não a pasta."""
    proc.falhar_etapa(
        conn, id_coleta, Etapa.NAVEGAR, Status.ERRO_LOGIN, mensagem,
    )


def registrar_download(conn, id_coleta: int, baixados: int) -> None:
    """Fecha a etapa BAIXAR e, com ela, o caso da varredura. `baixados` e
    somado ao que o caso ja tinha (R13)."""
    proc.registrar_contagem(conn, id_coleta, baixados=baixados)
    proc.concluir_etapa(conn, id_coleta, Etapa.BAIXAR)


# ---------------------------------------------------------------------------
# Caso 'invoice' — a nota
# ---------------------------------------------------------------------------

def save_invoice(
    conn,
    id_loja: int,
    file_path: Path,
    data: "InvoiceData",
    custo: float | None = None,
) -> tuple[int, int]:
    """Persiste a nota e suas linhas. Retorna (id_da_nota, linhas_inseridas).

    Cria o caso 'invoice' se ainda não existir e avança até LER. O nome do
    arquivo é o identificador do processo: reprocessar a mesma nota não duplica o caso.
    """
    id_processo = proc.abrir(
        conn,
        cod_tipo="invoice",
        identificador_processo=file_path.name,
        id_loja=id_loja,
        dt_origem=data.invoice_date,
    )
    proc.concluir_etapa(conn, id_processo, Etapa.COLETAR)

    soma_itens, fecha = _conferir_soma(data)

    confianca = data.reading_confidence
    revisar = confianca is not None and float(confianca) < CONFIANCA_MINIMA_PAINEL
    motivo_revisao = "baixa_confianca" if revisar else None

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {SCHEMA}.fat_invoice (
                id_processo, id_loja, nome_fornecedor, numero_invoice, dt_emissao,
                dt_vencimento, moeda, subtotal, imposto, total, arquivo,
                confianca, modelo_ia, custo_usd, observacao_ia,
                anotacao_manual_geral,
                soma_itens, fecha, revisar, motivo_revisao
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id_processo) DO UPDATE SET
                nome_fornecedor        = EXCLUDED.nome_fornecedor,
                numero_invoice          = EXCLUDED.numero_invoice,
                dt_emissao              = EXCLUDED.dt_emissao,
                total                  = EXCLUDED.total,
                confianca              = EXCLUDED.confianca,
                custo_usd              = EXCLUDED.custo_usd,
                anotacao_manual_geral  = EXCLUDED.anotacao_manual_geral,
                soma_itens             = EXCLUDED.soma_itens,
                fecha                  = EXCLUDED.fecha,
                revisar                = EXCLUDED.revisar,
                motivo_revisao         = EXCLUDED.motivo_revisao
            RETURNING id
            """,
            (
                id_processo, id_loja,
                truncar(data.supplier_name, 200),
                truncar(data.invoice_number, 80),
                data.invoice_date, data.due_date,
                data.currency or "USD",
                data.subtotal, data.tax_amount, data.total_amount,
                truncar(file_path.name, 200),
                data.reading_confidence,
                truncar(getattr(data, "model_ai", None), 80),
                custo if custo is not None else getattr(data, "cost_read", None),
                data.reading_notes,
                data.general_handwritten_notes,
                soma_itens, fecha, revisar, motivo_revisao,
            ),
        )
        id_invoice = cur.fetchone()[0]

        # Reprocesso apaga as linhas antigas: a leitura pode mudar de resultado.
        # Se a nota já passou pela conciliação ERP antes (FLUXO 4), `fat_
        # conciliacao_item.id_invoice_item` referencia essas linhas — sem
        # `ON DELETE CASCADE` nessa FK (só `id_conciliacao` tem), apagar
        # direto quebra com "violates foreign key constraint". Como os itens
        # em si vão sumir, a comparação antiga contra o PO já não faz mais
        # sentido de qualquer forma — apaga primeiro; a próxima conciliação
        # regrava `fat_conciliacao` (header) via `ON CONFLICT (id_invoice,
        # comparacao)`, então não precisa apagar o header aqui.
        cur.execute(
            f"""
            DELETE FROM {SCHEMA}.fat_conciliacao_item
             WHERE id_invoice_item IN (
                 SELECT id FROM {SCHEMA}.fat_invoice_item WHERE id_invoice = %s
             )
            """,
            (id_invoice,),
        )
        cur.execute(
            f"DELETE FROM {SCHEMA}.fat_invoice_item WHERE id_invoice = %s",
            (id_invoice,),
        )
        linhas = []
        for ordem, item in enumerate(data.items, start=1):
            qtd, preco, total = item.quantity, item.unit_price, item.total_price
            valor = total if total is not None else (
                qtd * preco if qtd is not None and preco is not None else None
            )
            linhas.append((
                id_invoice, ordem,
                truncar(item.description, 500),
                tipo_linha(item.description),
                qtd, truncar(item.unit, 30), preco, valor,
                getattr(item, "handwritten_notes", None),
                truncar(getattr(item, "item_code", None), 40),
                truncar(getattr(item, "upc", None), 20),
                getattr(item, "cases", None),
                truncar(getattr(item, "handwritten_code", None), 40),
            ))
        if linhas:
            cur.executemany(
                f"""
                INSERT INTO {SCHEMA}.fat_invoice_item
                    (id_invoice, ordem, descricao, tipo_linha, qtd, unidade,
                     preco_unit, valor_linha, anotacao_manual, item_code, upc,
                     caixas, codigo_manual)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                linhas,
            )
    conn.commit()

    # Leitura fraca (confiança < CONFIANCA_MINIMA_PAINEL) não trava a nota: ela
    # já saiu marcada com `revisar=true` no header e segue para a conciliação.
    # O piso vira fila de conferência no painel, não erro técnico.
    proc.concluir_etapa(conn, id_processo, Etapa.LER, custo=custo)

    return id_invoice, len(linhas)


def _conferir_soma(data: "InvoiceData") -> tuple[float | None, bool | None]:
    """Soma das linhas de mercadoria e se o cabeçalho fecha com elas (G9).

    `soma_itens` é só `tipo_linha = 'item'` (o que o painel soma). `fecha`
    compara `total` (ou `subtotal`, na falta) contra mercadoria + encargo —
    ajuste fica de fora (dwschiavon2_estrutura.md §4.3). `fecha = None` quando
    falta valor de linha ou não há cabeçalho: não dá para afirmar nem negar.
    """
    tem_item = False
    soma_item = 0.0
    soma_item_encargo = 0.0
    faltou_valor = False
    for item in data.items:
        qtd, preco, total_l = item.quantity, item.unit_price, item.total_price
        valor = total_l if total_l is not None else (
            qtd * preco if qtd is not None and preco is not None else None
        )
        tipo = tipo_linha(item.description)
        if tipo == "item":
            tem_item = True
        if tipo in ("item", "encargo"):
            if valor is None:
                faltou_valor = True
            else:
                soma_item_encargo += valor
                if tipo == "item":
                    soma_item += valor

    soma_itens = round(soma_item, 2) if tem_item else None

    base = data.total_amount if data.total_amount is not None else data.subtotal
    if faltou_valor or base is None or not data.items:
        return soma_itens, None
    tolerancia = max(abs(base) * _TOLERANCIA_SOMA_PCT, _TOLERANCIA_SOMA_MIN)
    return soma_itens, abs(base - soma_item_encargo) <= tolerancia


# ---------------------------------------------------------------------------
# Prefixo do arquivo → loja
# ---------------------------------------------------------------------------

# Os ids são os mesmos do dwschiavon: a migração preservou dim_loja.id.
_PREFIXO_LOJA: dict[str, tuple[int, str]] = {
    "wind": (1, "Windermere"),
    "drphil": (2, "Dr. Phillips"),
}


def config_from_filename(filename: str) -> tuple[int, str] | None:
    """Deriva (id_loja, nome_loja) do prefixo do arquivo baixado."""
    for prefixo, info in _PREFIXO_LOJA.items():
        if filename.lower().startswith(prefixo + "_"):
            return info
    return None

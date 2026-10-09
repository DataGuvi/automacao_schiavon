"""FLUXO 4 — Conciliação Invoice x PO do Catapult (ERP).

Busca no Catapult o(s) PO (Purchase Order) 'Ordered' do fornecedor da
invoice, raspa a grade Items de cada candidato, desempata pelos itens quando
há mais de um, e compara item a item — quantidade (Ordered/Received) e valor
(Invoiced Total Cost) — usando o motor de `conciliacao/reconcile_erp.py`.
Grava em `fat_conciliacao` / `fat_conciliacao_item` com `comparacao='erp'`.

Nota com anotação à mão de "insumo" não é levada ao Catapult — ver
`conciliacao/reconcile_erp.py::tem_anotacao_insumo`.

Vale para TODA invoice, carne ou não: é a única comparação que sempre existe
(ver `domain/categorias.py`). Substitui, como FLUXO 4, a antiga conciliação
contra a cotação semanal — `conciliacao_flow.py` continua no repo e
`crawler/controller.py` mantém a linha comentada, para o caso de essa
comparação voltar a ser necessária.

A raspagem do Catapult (login, busca de PO, grade Items) mora em
`commons/catapult/`, validada contra o ambiente real; o motor puro de match
em `commons/matcher.py`; a regra de uma invoice em
`conciliacao/reconcile_erp.py`. Aqui só a orquestração: uma sessão do
Catapult por loja (Windermere e Dr. Phillips têm URL própria), uma busca de
PO por invoice dentro dela.
"""

from __future__ import annotations

from datetime import date, timedelta

from commons.catapult import (
    extrair_prefixo_nome_po,
    fechar_browser,
    parar_playwright,
    fill_receiving_invoice_info,
    open_catapult_session,
    open_purchase_order,
    open_worksheets,
    scrape_po_items,
    search_purchase_orders_by_invoice,
    search_purchase_orders_by_supplier,
    to_po_lines,
)
from commons.datas import week_bounds
from commons.db import connect_db, fechar
from commons.docx_report import gerar_com_seguranca
from commons.exception import BusinessException, DataAccessException, IntegracaoException
from commons.logging_config import get_logger
from commons.matcher import POLine, norm_supplier
from commons.sheets import SheetsError
from conciliacao.pendentes_sinonimo import extrair_pendentes, sincronizar_pendentes
from conciliacao.reconcile_erp import (
    SKIPPED_INSUMO_ANNOTATION,
    escolher_po_por_itens,
    reconcile_items_against_po,
    tem_anotacao_insumo,
)
from conciliacao.relatorio_divergencia import NOME_LOJA, gerar_relatorio_divergencia_erp
from conciliacao.relatorio_sucesso import gerar_relatorio_sucesso_erp
from domain.service import notificacao_service
from domain.categorias import CategoriaFornecedor
from domain.config import Config
from domain.enums import Etapa, StatusExecucao as Status
from domain.service import processo_service as proc
from domain.service import sistema_service
from domain.service.conciliacao_service import (
    classificar_fornecedor,
    confirmar_fornecedor_para_aprender,
    escolher_nome_catapult,
    fetch_fornecedores,
    gravar_nome_catapult,
    montar_resolvedor_fornecedor,
    fetch_invoice_headers_aguardando_po,
    fetch_invoice_headers_for_reconciliation,
    fetch_invoice_headers_reprocesso,
    fetch_invoice_items_by_headers,
    fetch_item_sinonimos,
    fetch_supplier_aliases,
    ja_conciliada_erp,
    save_reconciliation_header,
    save_reconciliation_items,
)
from domain.sistemas import Sistema

log = get_logger(__name__)

# Acima disso, desempatar por itens (abrir + raspar CADA candidato) não
# escala — ver `_buscar_po`.
_MAX_CANDIDATOS_DESEMPATE = 20


def reconcile_erp_flow(
    config: Config,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict:
    """Roda a conciliação do período contra o PO do Catapult e grava o resultado."""
    if date_from is None or date_to is None:
        inicio, fim = week_bounds(date.today())
        date_from = date_from or inicio
        date_to = date_to or fim

    conn = connect_db(config.banco)
    try:
        log.info("Conciliacao Invoice x Catapult (ERP) - %s a %s", date_from, date_to)

        sinonimos = fetch_item_sinonimos(conn)
        aliases_invoice = fetch_supplier_aliases(conn, source="invoice")
        resolver_fornecedor = montar_resolvedor_fornecedor(
            fetch_fornecedores(conn), aliases_invoice,
        )

        # Semana anterior + atual em toda execucao, sem prender loja: a coleta
        # tambem varre as duas (spec-correcao-reconcile-erp R3). Nota ja
        # conciliada nao volta, entao so entra o que ainda esta pendente.
        inicio_ant, fim_ant = week_bounds(date_from - timedelta(days=7))
        log.info("Semana anterior - %s a %s", inicio_ant, fim_ant)
        pendentes_anterior = fetch_invoice_headers_for_reconciliation(conn, inicio_ant, fim_ant)
        headers_novos = fetch_invoice_headers_for_reconciliation(conn, date_from, date_to)
        reprocesso = fetch_invoice_headers_reprocesso(conn)
        aguardando_po = fetch_invoice_headers_aguardando_po(conn)
        headers = _selecionar_headers(pendentes_anterior, headers_novos, reprocesso, aguardando_po)

        if not headers:
            log.info("Nenhuma invoice no periodo.")
            return {"headers_total": 0}

        items_by_header = fetch_invoice_items_by_headers(conn, [h["id"] for h in headers])
        log.info("%s invoice(s) a conciliar contra o Catapult", len(headers))

        por_loja: dict[int, list[dict]] = {}
        for h in headers:
            por_loja.setdefault(h["id_loja"], []).append(h)

        totais = {
            "headers_total": 0, "headers_issue": 0,
            "items_total": 0, "items_issue": 0,
            "sem_po": 0, "aguardando_po": 0, "lojas_puladas": 0, "puladas_insumo": 0,
            "relatorios_gerados": 0, "relatorios_erro": 0, "notas_erro": 0,
            "sucessos_gerados": 0, "sucessos_erro": 0,
            "relatorios_cliente": [],
        }

        sheet_id = config.sinonimos_sheet_id or None
        for id_loja, headers_loja in por_loja.items():
            log.info("-- loja %s: %s nota(s) --", id_loja, len(headers_loja))
            _conciliar_loja(conn, id_loja, headers_loja, items_by_header, sinonimos,
                             resolver_fornecedor, aliases_invoice, config, sheet_id, totais)

        if totais.get("sheets_tentativas"):
            sem_erro = not totais.get("sheets_erro")
            sistema_service.registrar_acesso(
                conn, Sistema.GOOGLE_SHEETS, ok=sem_erro,
                mensagem=None if sem_erro else
                f"{totais['sheets_erro']} falha(s) sincronizando pendentes",
            )

        log.info("Invoices conciliadas : %s", totais['headers_total'])
        log.info("com alguma pendencia : %s", totais['headers_issue'])
        log.info("Itens comparados : %s", totais['items_total'])
        log.info("com pendencia : %s", totais['items_issue'])
        log.info("Sem PO no Catapult : %s", totais['sem_po'])
        log.info("Aguardando PO Ordered : %s", totais['aguardando_po'])
        log.info("Nao enviadas (insumo) : %s", totais['puladas_insumo'])
        log.info(
            "Relatorios divergencia : %s (%s falha(s))",
            totais['relatorios_gerados'], totais['relatorios_erro'],
        )
        log.info(
            "Relatorios sucesso : %s (%s falha(s))",
            totais['sucessos_gerados'], totais['sucessos_erro'],
        )
        if totais["lojas_puladas"]:
            log.info("Lojas puladas (sem credencial/login): %s", totais['lojas_puladas'])
        notificacao_service.enviar_relatorios_cliente(config, totais["relatorios_cliente"])
        return totais

    except BusinessException as exc:
        log.warning("conciliacao_erp: caso de negocio - %s", exc)
        return {"headers_total": 0, "erro_negocio": str(exc)}
    finally:
        fechar(conn)


def _selecionar_headers(
    pendentes_anterior: list[dict], headers_novos: list[dict], reprocesso: list[dict],
    aguardando_po: list[dict] | None = None,
) -> list[dict]:
    """Une as listas de notas a conciliar (semana anterior, atual, reprocesso
    explicito, status 56, e aguardando PO Ordered, status 13), sem duplicar por
    `id`. Nenhuma loja fica presa: as duas semanas entram sempre."""
    vistos: set[int] = set()
    headers = []
    for h in pendentes_anterior + headers_novos + reprocesso + (aguardando_po or []):
        if h["id"] not in vistos:
            vistos.add(h["id"])
            headers.append(h)
    return headers


def _conciliar_loja(
    conn, id_loja: int, headers: list[dict], items_by_header: dict[int, list[dict]],
    sinonimos: dict, resolver_fornecedor, aliases_invoice: dict[str, dict],
    config: Config, sheet_id: str | None, totais: dict,
) -> None:
    """Abre UMA sessão do Catapult para a loja e concilia todas as notas dela.

    Login ou tela Worksheets falhando são falha da SESSÃO — pula a loja
    inteira nesta execução (as notas ficam pendentes, `fetch_invoice_headers_
    for_reconciliation` devolve elas de novo no próximo run). Falha só numa
    invoice específica (PO não abriu, busca não aplicou o filtro) não derruba
    a sessão — ver `_conciliar_invoice`.

    Roda headless por padrão, porque o pipeline roda sem ninguém olhando;
    `ECRS_HEADLESS=false` no profile abre o navegador visível para depurar
    seletor (era o valor fixo no código antes, o que travava a execução
    agendada num host sem sessão gráfica).

    Captura `IntegracaoException`, que é o que `commons/catapult` levanta em
    QUALQUER falha técnica — `CatapultLoginError` inclusive, por herança.
    Antes capturava `(CatapultLoginError, RuntimeError)`, e o
    `playwright...TimeoutError`, que não é `RuntimeError`, escapava daqui e
    derrubava o fluxo inteiro com as lojas seguintes sem conciliar.
    """
    ecrs = config.ecrs
    url = ecrs.url_da_loja(id_loja)
    access_email = ecrs.access_email
    usuario = ecrs.usuario
    senha = ecrs.senha
    if not all((url, access_email, usuario, senha)):
        log.warning(
            "id_loja=%s: credencial/URL do Catapult ausente, pulando %s nota(s)",
            id_loja, len(headers),
        )
        totais["lojas_puladas"] += 1
        return

    pw = browser = None
    try:
        pw, browser, page = open_catapult_session(
            url, access_email, usuario, senha,
            headless=ecrs.headless,
        )
        sistema_service.registrar_acesso(conn, Sistema.ERP_CATAPULT, ok=True)
        for header in headers:
            _conciliar_invoice(
                page, url, conn, header, items_by_header.get(header["id"], []),
                sinonimos, resolver_fornecedor, aliases_invoice, sheet_id, totais,
            )
    except IntegracaoException as exc:
        log.error("id_loja=%s: sessao do Catapult falhou - %s", id_loja, exc)
        notificacao_service.registrar_erro(f"Catapult loja {id_loja}: login/sessao", exc)
        sistema_service.registrar_acesso(conn, Sistema.ERP_CATAPULT, ok=False, mensagem=str(exc))
        totais["lojas_puladas"] += 1
    except DataAccessException as exc:
        log.exception("id_loja=%s: falha de banco, loja interrompida", id_loja)
        notificacao_service.registrar_erro(f"BD loja {id_loja}: loja interrompida", exc)
        totais["lojas_puladas"] += 1
    finally:
        _fechar_navegador(pw, browser)


def _fechar_navegador(pw, browser) -> None:
    """Limpeza que nunca levanta. `pw`/`browser` podem ser None (login falhou cedo).

    Delega para `commons/catapult`, que já tem esses dois passos — era a
    terceira cópia do mesmo `close`/`stop` no projeto.
    """
    fechar_browser(browser)
    parar_playwright(pw)


def _conciliar_invoice(
    page, url: str, conn, header: dict, items: list[dict], sinonimos: dict,
    resolver_fornecedor, aliases_invoice: dict[str, dict],
    sheet_id: str | None, totais: dict,
) -> None:
    """Concilia UMA invoice contra o PO dela. Nunca propaga — falha desta nota
    não derruba a loja nem as seguintes.

    `IntegracaoException` (Catapult) vira `falhar_etapa`. `DataAccessException`
    (o service já fez rollback) e o imprevisto só são logados e contados: o
    banco pode ser justamente o que falhou, então não se tenta gravar status.
    """
    nota = header.get("invoice_number")
    try:
        _processar_invoice(
            page, url, conn, header, items, sinonimos,
            resolver_fornecedor, aliases_invoice, sheet_id, totais,
        )
    except IntegracaoException as exc:
        log.error("nota %s: falha raspando o Catapult - %s", nota, exc)
        notificacao_service.registrar_erro(f"Catapult nota {nota}: elemento/raspagem", exc)
        totais["notas_erro"] += 1
        _marcar_erro_navegacao(conn, header, str(exc))
    except DataAccessException as exc:
        log.exception("nota %s: falha de banco gravando a conciliacao", nota)
        notificacao_service.registrar_erro(f"BD nota {nota}: gravando conciliacao", exc)
        totais["notas_erro"] += 1
    except Exception as exc:  # noqa: BLE001 — rede de seguranca do laco de item
        log.exception("nota %s: erro inesperado na conciliacao", nota)
        notificacao_service.registrar_erro(f"nota {nota}: erro inesperado", exc)
        totais["notas_erro"] += 1


def _marcar_erro_navegacao(conn, header: dict, mensagem: str) -> None:
    """Grava `ERRO_NAVEGACAO` na nota. Nunca levanta.

    Roda dentro de um `except` de `_conciliar_invoice`: uma `DataAccessException`
    aqui escaparia dos `except` irmaos e derrubaria a loja inteira.
    """
    try:
        proc.falhar_etapa(
            conn, header["id_processo"], Etapa.CONCILIAR_ERP,
            Status.ERRO_NAVEGACAO, mensagem,
        )
    except DataAccessException:
        log.exception(
            "nota %s: falha de banco marcando ERRO_NAVEGACAO", header.get("invoice_number"),
        )


def _processar_invoice(
    page, url: str, conn, header: dict, items: list[dict], sinonimos: dict,
    resolver_fornecedor, aliases_invoice: dict[str, dict],
    sheet_id: str | None, totais: dict,
) -> None:
    if not items:
        # Nota sem linha de mercadoria: nada a comparar. Encerra o caso, senão
        # ela volta a cada execucao e prende a loja na semana anterior.
        log.warning("nota %s: sem itens, encerrando sem conciliar", header.get("invoice_number"))
        proc.falhar_etapa(
            conn, header["id_processo"], Etapa.CONCILIAR_ERP,
            Status.ENCERRADO_SEM_ARQUIVO, "Nota sem linhas de item.",
        )
        return

    supplier_name = header.get("supplier_name")
    fornecedor = resolver_fornecedor(supplier_name or "")
    categoria_fornecedor = _categoria_do_fornecedor(supplier_name, aliases_invoice, fornecedor)

    if categoria_fornecedor == CategoriaFornecedor.INSUMO:
        _gravar_skip_insumo(conn, header, totais)
        return

    if tem_anotacao_insumo(items, header.get("general_handwritten_notes")):
        _aprender_insumo(conn, fornecedor, supplier_name, aliases_invoice)
        _gravar_skip_insumo(conn, header, totais)
        return

    po_lines = _buscar_po(page, url, header, items, sinonimos, resolver_fornecedor, conn=conn)

    # `None` (busca não achou PO nenhum pro fornecedor) e `[]` (achou
    # candidato(s), mas não deu pra desempatar) chegam iguais em
    # `reconcile_items_against_po`: nota inteira sem PO pra comparar —
    # `PO_NAO_ENCONTRADA` no header e em cada item.
    if po_lines is None and proc.aguardar_po_ordered(conn, header["id_processo"], Etapa.CONCILIAR_ERP):
        # Nenhuma PO 'Ordered' agora: fica em PO_NAO_ENCONTRADA e so volta a
        # pesquisar na PROXIMA execucao do robo (spec-retentativa-po-ordered).
        log.info("nota %s: sem PO Ordered, nova consulta na proxima execucao", header.get("invoice_number"))
        totais["aguardando_po"] += 1
        return

    if not po_lines:
        po_lines = []
        totais["sem_po"] += 1

    resultado = reconcile_items_against_po(
        items, po_lines, sinonimos=sinonimos,
        supplier_name=supplier_name, categoria_fornecedor=categoria_fornecedor,
    )
    _gravar_resultado(conn, header, resultado, sheet_id, totais)


def _categoria_do_fornecedor(
    supplier_name: str | None, aliases_invoice: dict[str, dict], fornecedor: dict | None,
) -> str | None:
    """Categoria da nota: a do alias `invoice` exato; sem alias, a do fornecedor
    resolvido por aproximacao (spec-categoria-insumo-carne R2). `insumo` pula a
    nota em silencio, entao por aproximacao so vale com nome confirmado (R7)."""
    alias = aliases_invoice.get(norm_supplier(supplier_name), {})
    if alias.get("categoria"):
        return alias["categoria"]
    categoria = (fornecedor or {}).get("categoria")
    if categoria == CategoriaFornecedor.INSUMO and not confirmar_fornecedor_para_aprender(
        supplier_name or "", fornecedor,
    ):
        return None
    return categoria


def _aprender_insumo(
    conn, fornecedor: dict | None, supplier_name: str | None, aliases_invoice: dict[str, dict],
) -> None:
    """A Vision viu 'insumo' na nota: grava `categoria='insumo'` no fornecedor, so se
    foi resolvido com confianca e ainda nao tem categoria (R3). Nunca levanta."""
    if conn is None or fornecedor is None:
        return
    por_alias = norm_supplier(supplier_name) in aliases_invoice
    if not (por_alias or confirmar_fornecedor_para_aprender(supplier_name or "", fornecedor)):
        return
    try:
        if classificar_fornecedor(
            conn, fornecedor["id"], CategoriaFornecedor.INSUMO, somente_sem_categoria=True,
        ):
            fornecedor["categoria"] = CategoriaFornecedor.INSUMO
            log.info("fornecedor %s: categoria=insumo (nota com anotacao de insumo)", fornecedor["id"])
    except DataAccessException:
        log.exception("fornecedor %s: falha gravando categoria insumo", fornecedor.get("id"))


def _aprender_nome_catapult(
    conn, fornecedor: dict | None, supplier_name: str, achados: list[dict],
) -> None:
    """Grava o alias 'erp' do fornecedor quando o fornecedor resolvido E um
    prefixo de PO da grade casam a >= 95% com o nome da invoice. Nunca levanta: aprender e secundario."""
    if conn is None or fornecedor is None:
        return
    if not confirmar_fornecedor_para_aprender(supplier_name, fornecedor):
        return
    try:
        prefixos = sorted({extrair_prefixo_nome_po(a["name"]) for a in achados})
        nome = escolher_nome_catapult(supplier_name, prefixos)
        if nome and nome != fornecedor.get("nome_catapult"):
            if gravar_nome_catapult(conn, fornecedor["id"], nome):
                log.info("fornecedor %s: alias erp=%s", fornecedor["id"], nome)
                fornecedor["nome_catapult"] = nome
            else:
                log.warning(
                    "fornecedor %s: alias erp %r ja e de outro fornecedor",
                    fornecedor["id"], nome,
                )
    except DataAccessException:
        log.exception("fornecedor %s: falha gravando alias erp", fornecedor.get("id"))


def _buscar_po(
    page, url: str, header: dict, items: list[dict], sinonimos: dict,
    resolver_fornecedor, conn=None,
) -> list[POLine] | None:
    """Acha o PO desta invoice no Catapult e devolve os itens já convertidos.

    `None` é resultado normal — a busca por fornecedor não devolveu PO
    nenhum. Lista VAZIA (`[]`) também é normal, mas por outro motivo — achou
    candidato(s), só não deu pra desempatar (candidatos demais, ou nenhum
    casou pelos itens). Os dois chegam iguais em `_conciliar_invoice`: nota
    sem PO pra comparar, `reconcile_items_against_po` soma `PO_NAO_ENCONTRADA`
    no HEADER, sem marcar item nenhum individualmente. Só propaga
    `IntegracaoException` quando a própria busca/raspagem falhou de um jeito
    técnico (filtro não aplicou, grade não carregou, timeout do Playwright).

    Sempre volta pra tela Worksheets antes de buscar: `open_purchase_order`
    (nota anterior) navega pra FORA dela via `page.goto(href)`, e o dropdown
    de categoria que a busca depende nem existe fora dessa tela — sem isto,
    a segunda nota em diante de cada loja falhava com timeout (achado rodando
    o benchmark manual, `manutencao/benchmark_conciliacao_erp.py`).

    Preenche Invoice Number/Invoice Date na aba Receiving Information do PO
    ANTES de raspar Items/conciliar (`fill_receiving_invoice_info`) — regra de
    negócio nova: o número/data da nota vai pro Catapult sempre, mesmo quando
    a comparação abaixo vai acusar divergência, porque a identificação da nota
    não depende do resultado da conciliação de itens.
    """
    supplier_name = header.get("supplier_name") or ""
    invoice_number = str(header.get("invoice_number") or "").strip()
    open_worksheets(page, url)

    # Busca prioritaria: 'Invoice Reference' + 'Equals' com o numero da nota.
    # So se nao achar PO que case pelos itens cai na busca por nome (abaixo).
    if invoice_number:
        achados = search_purchase_orders_by_invoice(page, invoice_number)
        escolha = _desempatar_por_itens(
            page, achados, items, sinonimos, f"invoice {invoice_number!r}",
        )
        if escolha is not None:
            return _abrir_po_escolhido(page, header, achados, *escolha)
        if 0 < len(achados) <= _MAX_CANDIDATOS_DESEMPATE:
            open_worksheets(page, url)  # os POs abertos tiraram a tela da busca
        log.info("nota %s: sem PO pela Invoice Reference, buscando pelo nome", invoice_number)

    # O nome lido da invoice frequentemente diverge do nome que o Catapult
    # conhece (ele trunca/abrevia, ex. 'Freshpoint Central FL' -> 'Fresh
    # Poin') — busca pelo alias 'erp' (`dim_fornecedor_alias`) do fornecedor
    # resolvido (alias invoice ou fuzzy); sem cadastro, cai pro nome cru da
    # invoice.
    fornecedor = resolver_fornecedor(supplier_name)
    termo_busca = (fornecedor or {}).get("nome_catapult") or supplier_name
    achados = search_purchase_orders_by_supplier(
        page, termo_busca, nomes_alternativos=(supplier_name,),
    )
    if not achados:
        return None
    _aprender_nome_catapult(conn, fornecedor, supplier_name, achados)

    escolha = _desempatar_por_itens(
        page, achados, items, sinonimos, f"{termo_busca!r} (fornecedor lido: {supplier_name!r})",
    )
    if escolha is None:
        return []
    return _abrir_po_escolhido(page, header, achados, *escolha)


def _desempatar_por_itens(
    page, achados: list[dict], items: list[dict], sinonimos: dict, busca: str,
) -> tuple[int, list[list[POLine]]] | None:
    """Abre cada PO candidato, raspa os itens e escolhe o que casa com a nota.

    `None` = sem candidato, candidatos demais ou nenhum casou pelos itens.
    Vale também para um PO só: um 'Ordered' aberto do fornecedor não é
    necessariamente o desta nota, e `fill_receiving_invoice_info` escreve
    número/data da nota no Catapult de produção — só depois de conferir
    que os itens casam.
    """
    if not achados:
        return None
    if len(achados) > _MAX_CANDIDATOS_DESEMPATE:
        # Fornecedor de entrega frequente (ex. FreshPoint, 199+ PO 'Ordered'
        # num store só) — abrir e raspar cada candidato pra desempatar por
        # itens não escala (seriam centenas de navegações). Sem um jeito
        # mais barato de pré-filtrar (a grade de busca não expõe data
        # sem abrir o PO), trata como ambíguo demais em vez de travar a
        # loja inteira nessa nota.
        log.warning(
            "%s PO(s) 'Ordered' para %s - mais que %s, nao da pra desempatar abrindo um por um - tratando como sem PO",
            len(achados), busca, _MAX_CANDIDATOS_DESEMPATE,
        )
        return None

    candidatos: list[list[POLine]] = []
    for achado in achados:
        open_purchase_order(page, achado["href"])
        candidatos.append(to_po_lines(scrape_po_items(page)))
    indice = escolher_po_por_itens(items, candidatos, sinonimos)
    if indice is None:
        log.warning(
            "%s PO(s) 'Ordered' para %s, nenhum casou com os itens desta nota - tratando como sem PO",
            len(achados), busca,
        )
        return None
    return indice, candidatos


def _abrir_po_escolhido(
    page, header: dict, achados: list[dict], indice: int, candidatos: list[list[POLine]],
) -> list[POLine]:
    """Abre o PO escolhido, grava número/data da nota nele e devolve os itens."""
    open_purchase_order(page, achados[indice]["href"])
    invoice_number = str(header.get("invoice_number") or "").strip()
    if invoice_number and header.get("invoice_date"):
        fill_receiving_invoice_info(page, invoice_number, header["invoice_date"])
    else:
        log.warning(
            "nota id=%s: sem numero ou data, nao preenche Receiving Information no Catapult",
            header.get("id"),
        )
    return candidatos[indice]


def _gravar_skip_insumo(conn, header: dict, totais: dict) -> None:
    """Grava a nota como 'pulada' — anotação à mão de insumo, não vai para o
    Catapult (ver `tem_anotacao_insumo`). Sem itens comparados (não houve
    busca de PO nenhuma), `has_issue`/`needs_review` False — é uma exclusão
    intencional, não uma divergência a revisar."""
    header_row = {
        "id_invoice_header": header["id"],
        "id_loja": header["id_loja"],
        "id_processo": header["id_processo"],
        "comparacao": "erp",
        "issue_codes": [SKIPPED_INSUMO_ANNOTATION],
        "has_issue": False,
        "needs_review": False,
    }
    save_reconciliation_header(conn, header_row)
    proc.concluir_etapa(conn, header["id_processo"], Etapa.CONCILIAR_ERP, com_alerta=False)

    totais["headers_total"] += 1
    totais["puladas_insumo"] += 1
    log.info("nota %s: anotacao de insumo - nao enviada ao Catapult", header.get('invoice_number'))


def _gravar_resultado(
    conn, header: dict, resultado: dict, sheet_id: str | None, totais: dict,
) -> None:
    header_row = {
        "id_invoice_header": header["id"],
        "id_loja": header["id_loja"],
        "id_processo": header["id_processo"],
        "comparacao": "erp",
        "issue_codes": resultado["issue_codes"],
        "has_issue": resultado["has_issue"],
        "needs_review": resultado["needs_review"],
    }
    # Antes de gravar: nota ja conciliada nao reenvia e-mail (spec RF-06).
    ja_reportada = ja_conciliada_erp(conn, header["id"])
    recon_header_id = save_reconciliation_header(conn, header_row)
    save_reconciliation_items(conn, recon_header_id, resultado["items"])
    _sincronizar_pendentes(header, resultado["items"], sheet_id, totais)
    # Conclui a etapa ANTES dos relatorios: se `concluir_etapa` falhar, a nota
    # volta na proxima execucao sem ter gerado (e reenviado) .docx duplicado.
    proc.concluir_etapa(
        conn, header["id_processo"], Etapa.CONCILIAR_ERP,
        com_alerta=resultado["needs_review"],
    )
    _gerar_relatorios(header, resultado, totais, notificar=not ja_reportada)

    totais["headers_total"] += 1
    if resultado["has_issue"]:
        totais["headers_issue"] += 1
    com_issue = sum(1 for r in resultado["items"] if r["has_issue"])
    totais["items_total"] += len(resultado["items"])
    totais["items_issue"] += com_issue

    if resultado["po_orphans"]:
        log.info(
            "(%s item(ns) do PO nao reclamado(s) por nenhuma linha desta nota - recebido/pedido mas nao faturado aqui)",
            len(resultado['po_orphans']),
        )

    status = "DIVERGE" if resultado["has_issue"] else "OK"
    log.info(
        "%s nota %s %s item(ns), %s com diferenca",
        status, header.get('invoice_number'), len(resultado['items']), com_issue,
    )


def _gerar_relatorios(
    header: dict, resultado: dict, totais: dict, notificar: bool = True,
) -> None:
    """Gera o .docx desta invoice: divergência (`conciliacao/relatorio_
    divergencia.py`) se algum item diverge, sucesso (`conciliacao/relatorio_
    sucesso.py`) se a nota inteira bateu — os dois são mutuamente exclusivos,
    cada gerador devolve `None` quando não é a vez dele. Secundário ao
    resultado principal, já gravado no banco — falha aqui não derruba a
    conciliação da nota; só conta em `totais` para aparecer no resumo.
    `notificar=False` (nota já reportada antes) regera o .docx mas não o põe na
    fila de e-mail ao cliente."""
    nota = header.get("invoice_number")
    divergencia, falhou = gerar_com_seguranca(
        lambda: gerar_relatorio_divergencia_erp(header, resultado),
        f"divergencia nota {nota}",
    )
    totais["relatorios_erro"] += falhou
    totais["relatorios_gerados"] += divergencia is not None
    if falhou:
        notificacao_service.registrar_erro(f"relatorio .docx divergencia nota {nota}",
                                           mensagem="falha gerando o .docx")

    sucesso, falhou = gerar_com_seguranca(
        lambda: gerar_relatorio_sucesso_erp(header, resultado),
        f"sucesso nota {nota}",
    )
    totais["sucessos_erro"] += falhou
    totais["sucessos_gerados"] += sucesso is not None
    if falhou:
        notificacao_service.registrar_erro(f"relatorio .docx sucesso nota {nota}",
                                           mensagem="falha gerando o .docx")
    gerado = divergencia or sucesso
    if gerado is not None and not notificar:
        log.info("nota %s: ja reportada ao cliente antes, e-mail nao reenviado", nota)
    elif gerado is not None:
        itens = resultado["items"]
        totais["relatorios_cliente"].append(notificacao_service.RelatorioNota(
            invoice=str(nota or f"id{header['id']}"),
            fornecedor=str(header.get("supplier_name") or "-"),
            loja=NOME_LOJA.get(header.get("id_loja"), "-"),
            divergencia=divergencia is not None,
            caminho=gerado, itens=len(itens),
            itens_divergentes=sum(1 for i in itens if i["has_issue"]),
        ))


def _sincronizar_pendentes(
    header: dict, items: list[dict], sheet_id: str | None, totais: dict,
) -> None:
    """Registra na aba `Pendentes` os itens desta nota sem par no PO.

    Secundário ao resultado principal: nunca propaga. `SheetsError` (API fora
    do ar, credencial ausente, aba renomeada) vira contagem em `totais` — o
    acesso agregado ao Sheets é registrado uma vez só, no fim de
    `reconcile_erp_flow` (ver `totais['sheets_*']`), não a cada nota.
    """
    if not sheet_id:
        return
    try:
        pendentes = extrair_pendentes(
            items, header.get("supplier_name") or "", "erp",
            header.get("invoice_date") or date.today(),
        )
        n = sincronizar_pendentes(sheet_id, pendentes)
        totais["sheets_tentativas"] = totais.get("sheets_tentativas", 0) + 1
        if n:
            log.info("(%s pendencia(s) nova(s) na aba Pendentes)", n)
    except SheetsError as exc:
        totais["sheets_tentativas"] = totais.get("sheets_tentativas", 0) + 1
        totais["sheets_erro"] = totais.get("sheets_erro", 0) + 1
        log.warning("falha sincronizando pendentes no Sheets: %s", exc)


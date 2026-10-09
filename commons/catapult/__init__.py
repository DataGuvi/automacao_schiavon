"""Sessão autenticada no ERP Catapult — Cloudflare Access + Playwright.

O Catapult fica atrás do Cloudflare Access: a página pede um e-mail, manda um
código de uso único (OTP) para a caixa correspondente e só libera acesso
depois do código confirmado. O código é lido via `commons/gmail.fetch_otp_code`
(a caixa em si é autorizada uma vez com `manutencao/gmail_oauth_setup.py`).

ATENÇÃO — mapeamento ainda não confirmado contra a tela real (ver `TASKS.md`
Fase 0). Os seletores abaixo são o padrão conhecido do Cloudflare Access
(mesmo formulário em qualquer app atrás dele), não foram testados contra o
Catapult de verdade. Para depurar um seletor errado, rode com o navegador
visível (`ECRS_HEADLESS=false` no profile, ver `reconcile_erp_flow`); em
qualquer modo `_debug_dump` loga a url da falha (sem gravar screenshot/HTML).

Depois do Cloudflare Access, o Catapult pede login próprio (usuário/senha do
ECRS) — tela GWT, seletores confirmados contra o ambiente real (`login()`).

**Toda falha daqui sai classificada** como `IntegracaoException` (ou
`CatapultLoginError`, que é uma subclasse dela): este módulo é a fronteira
com um sistema externo, e é aqui que o erro do Playwright — `TimeoutError`,
elemento destacado, navegador que não subiu — deixa de ser exceção de
biblioteca e passa a ser falha de integração que o fluxo sabe tratar. Sem
isso o erro subia cru: `playwright...TimeoutError` **não** é `RuntimeError`,
escapava do `except` de quem chamava e derrubava as lojas seguintes.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from playwright.sync_api import Error as PwError
from playwright.sync_api import TimeoutError as PwTimeout

from commons.exception import IntegracaoException
from commons.gmail import GmailOtpTimeout, fetch_otp_code
from commons.logging_config import get_logger
from commons.matcher import POLine, aceitar_prefixos_catapult

log = get_logger(__name__)

__all__ = [
    "CatapultLoginError", "handle_cloudflare_access", "login", "open_catapult_session",
    "open_worksheets", "search_purchase_orders_by_supplier", "open_purchase_order",
    "extrair_prefixo_nome_po", "fill_receiving_invoice_info", "scrape_po_items", "to_po_lines",
    "fechar_browser", "parar_playwright",
]

# Remetente do código de acesso — confirmado em teste real (não é seletor,
# é infraestrutura do próprio Cloudflare, estável entre apps/clientes).
CLOUDFLARE_OTP_SENDER = "noreply@notify.cloudflare.com"

# Seletores do login do Catapult (tela GWT) — confirmados contra o ambiente real.
_SEL_USERNAME = 'xpath=//input[@id="gwt-debug-TextBox-username"]'
_SEL_PASSWORD = 'xpath=//input[@id="gwt-debug-PasswordTextBox-password"]'
_SEL_BOTAO_LOGIN = 'xpath=//button[@id="gwt-debug-Button-login"]'


class CatapultLoginError(IntegracaoException):
    """Falha de autenticação no Catapult (Cloudflare Access ou credencial).

    Distinta de erro de navegação, no mesmo espírito de
    `commons.sharepoint.SharePointLoginError` — quem chama grava
    `ERRO_LOGIN` e alerta a operação sobre qual sistema caiu.

    É `IntegracaoException` (antes era `RuntimeError`): sistema externo fora
    de alcance aborta o fluxo e chama `monitor.registrar_acesso(ok=False)`,
    que é exatamente o que `reconcile_erp_flow._conciliar_loja` faz. Quem só
    quer distinguir "não logou" de "não navegou" continua capturando esta
    classe; quem quer as duas captura `IntegracaoException`.
    """


def handle_cloudflare_access(page, email: str, otp_timeout: int = 90) -> None:
    """Preenche o desafio do Cloudflare Access, se a página cair nele.

    Fluxo: e-mail -> "Send me a code" -> código chega por e-mail -> preenche
    -> segue para a aplicação (Catapult). Não faz nada se a página não
    estiver no domínio do Access (mesmo padrão de
    `_handle_microsoft_login`, que sai cedo se não houver redirect).
    """
    if "cloudflareaccess.com" not in page.url and "/cdn-cgi/access/" not in page.url:
        return

    desde = _pedir_codigo(page, email)
    codigo = _ler_codigo(page, desde, otp_timeout)
    _confirmar_codigo(page, codigo)
    log.info("cloudflare: acesso liberado")


def _pedir_codigo(page, email: str) -> datetime:
    """Preenche o e-mail e dispara o envio do código. Devolve o instante do
    pedido, que `_ler_codigo` usa para não pegar um código de tentativa
    anterior na caixa."""
    log.info("cloudflare: preenchendo e-mail de acesso")
    try:
        page.wait_for_selector('input[type="email"]', timeout=15_000)
        page.fill('input[type="email"]', email)
        page.keyboard.press("Enter")

        # Marca o instante ANTES de esperar a tela de código: o e-mail pode
        # levar alguns segundos, e queremos só o código desta tentativa.
        desde = datetime.now(timezone.utc)
        page.wait_for_selector(
            'input[name="code"], input[autocomplete="one-time-code"]',
            timeout=15_000,
        )
    except PwError as exc:
        _debug_dump(page, "cloudflare_email")
        raise CatapultLoginError(
            f"tela de e-mail do Cloudflare Access não respondeu como esperado: {exc}"
        ) from exc
    return desde


def _ler_codigo(page, desde: datetime, otp_timeout: int) -> str:
    """Espera o código de uso único chegar na caixa e devolve."""
    log.info("cloudflare: e-mail enviado, aguardando codigo na caixa")
    try:
        return fetch_otp_code(CLOUDFLARE_OTP_SENDER, desde, timeout=otp_timeout)
    except GmailOtpTimeout as exc:
        _debug_dump(page, "cloudflare_otp_timeout")
        raise CatapultLoginError(str(exc)) from exc


def _confirmar_codigo(page, codigo: str) -> None:
    """Digita o código e espera sair do domínio do Access."""
    log.info("cloudflare: codigo recebido, confirmando acesso")
    try:
        page.fill('input[name="code"], input[autocomplete="one-time-code"]', codigo)
        page.keyboard.press("Enter")
        page.wait_for_url(
            lambda url: "cloudflareaccess.com" not in url and "/cdn-cgi/access/" not in url,
            timeout=20_000,
        )
    except PwError as exc:
        _debug_dump(page, "cloudflare_code_submit")
        raise CatapultLoginError(
            f"código enviado mas o Cloudflare Access não liberou o acesso: {exc}"
        ) from exc


def login(page, username: str, password: str, timeout: int = 20_000) -> None:
    """Preenche o login do Catapult (usuário, senha, botão) e espera sair da tela.

    Assume que o Cloudflare Access já foi resolvido (`handle_cloudflare_access`).
    Espera o campo de usuário DESAPARECER como sinal de sucesso — a tela é GWT
    (SPA), não há redirect de URL para esperar como no Microsoft login.
    """
    log.info("[catapult] Preenchendo login...")
    try:
        page.wait_for_selector(_SEL_USERNAME, timeout=timeout)
        page.fill(_SEL_USERNAME, username)
        page.fill(_SEL_PASSWORD, password)
        page.click(_SEL_BOTAO_LOGIN)
        _confirmar_sessao_ativa_se_aparecer(page)
        page.wait_for_selector(_SEL_USERNAME, state="detached", timeout=timeout)
    except PwError as exc:
        _debug_dump(page, "catapult_login")
        raise CatapultLoginError(
            f"login do Catapult não respondeu como esperado: {exc}"
        ) from exc

    log.info("[catapult] Login concluido.")


def _confirmar_sessao_ativa_se_aparecer(page, timeout: int = 5_000) -> None:
    """Confirma o modal "This ID already has an active session..." quando ele
    aparece — sessão anterior derrubada sem logout limpo (browser fechado
    direto) deixa a sessão presa do lado do servidor, e o Catapult pergunta se
    pode encerrá-la antes de liberar um novo login. Não aparece em login
    normal — nesse caso só segue sem fazer nada (timeout curto, de propósito).

    O `botao.click()` final fica fora do `try` de propósito: só é chamada de
    dentro do `try` de `login()`, então um `PwError` dele já sai como
    `CatapultLoginError` — que é a classificação certa (o login não passou),
    e não a que esta função saberia dar."""
    try:
        botao = page.get_by_role("button", name="Yes")
        botao.wait_for(state="visible", timeout=timeout)
    except PwTimeout:
        return
    log.info("[catapult] Sessao anterior presa, encerrando antes de continuar...")
    botao.click()


def open_catapult_session(
    url: str, access_email: str, username: str, password: str, headless: bool = True,
):
    """Abre uma sessão Playwright autenticada no Catapult: Access + login ECRS.

    Retorna (playwright, browser, page) — o chamador deve fechar com
    `browser.close()`. Mesmo contrato de `commons.sharepoint.open_sharepoint_session`.
    Se o login falhar, fecha o browser antes de propagar o erro — sem isso o
    processo do Chromium fica órfão, já que quem chamou nunca recebe a
    referência pra fechar.
    """
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = None
    try:
        # `channel="chromium"` so em headless: o Playwright usa por padrao o
        # `chromium-headless-shell`, um binario diferente do Chromium com janela,
        # e nele a tela Worksheets do Catapult (GWT) nao chegou a montar — o
        # conteudo ficou vazio e o `wait_for_selector` estourou. Com o canal
        # "chromium" o headless roda o mesmo navegador da execucao com janela.
        browser = pw.chromium.launch(
            headless=headless, channel="chromium" if headless else None,
        )
        # Viewport largo de propósito: abaixo de ~1920 de largura a barra de
        # ferramentas do PO (Print Labels/Export/.../Save/Cancel) colapsa num
        # menu "Actions" — `fill_receiving_invoice_info` conta com os botões
        # individuais visíveis. 1920x945 é o tamanho confirmado contra o
        # Catapult real onde a barra NÃO colapsa (1600x900 ainda colapsou).
        page = browser.new_page(viewport={"width": 1920, "height": 945})
        _observar_falhas_da_pagina(page)
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        handle_cloudflare_access(page, access_email)
        login(page, username, password)
    except PwError as exc:
        # `launch` entrou no try de propósito: o Chromium pode não subir
        # (binário não instalado, sem display) e antes isso escapava ANTES do
        # try — sem classificação e sem `pw.stop()`, deixando o processo do
        # Playwright órfão.
        _fechar(pw, browser)
        raise IntegracaoException(
            f"não foi possível abrir a sessão do Catapult em {url}: {exc}"
        ) from exc
    except Exception:
        # `CatapultLoginError` e o resto sobem com o tipo que já têm; aqui só
        # garantimos que o navegador não fica aberto atrás deles.
        _fechar(pw, browser)
        raise

    log.info("[catapult] Sessao aberta.")
    return pw, browser, page


def _observar_falhas_da_pagina(page) -> None:
    """Loga erro de console e requisicao que falhou, no momento em que ocorrem.

    O `_debug_dump` so guarda o DOM final: quando a tela nao monta (como em
    headless), o DOM vazio nao diz POR QUE. Estes dois eventos dizem — erro de
    JS da aplicacao ou chamada de rede que caiu. Os handlers so logam, nunca
    levantam.
    """
    page.on("console", lambda msg: log.warning(
        "[catapult][console] %s", msg.text[:300]) if msg.type == "error" else None)
    page.on("requestfailed", lambda req: log.warning(
        "[catapult][rede] %s falhou: %s", req.url[:150], req.failure))


def _fechar(pw, browser) -> None:
    """Limpeza que nunca levanta — chamada quando `open_catapult_session` falha.

    `browser` pode ser None (o `launch` foi o que falhou). Os dois passos são
    funções separadas para nenhuma delas ter mais de um `try`, e porque um
    precisa rodar mesmo que o outro falhe: browser aberto sem `pw.stop()`
    deixa processo órfão.
    """
    fechar_browser(browser)
    parar_playwright(pw)


def fechar_browser(browser) -> None:
    """Fecha o navegador. Nunca levanta."""
    if browser is None:
        return
    try:
        browser.close()
    except Exception:  # noqa: BLE001 — limpeza
        log.warning("catapult: falha ao fechar o browser", exc_info=True)


def parar_playwright(pw) -> None:
    """Encerra o driver do Playwright. Nunca levanta."""
    if pw is None:
        return
    try:
        pw.stop()
    except Exception:  # noqa: BLE001 — limpeza
        log.warning("catapult: falha ao parar o playwright", exc_info=True)


def _debug_dump(page, tag: str) -> None:
    """Loga a url no momento da falha, pra diagnostico. Nao grava arquivo."""
    try:
        log.info("[debug] falha em %s, url: %s", tag, page.url)
    except Exception as e:
        log.error("[debug] falhou ao ler a url: %s", e)


# =============================================================================
# Purchase Order — busca por fornecedor e raspagem da grade de itens
#
# Seletores confirmados contra o Catapult real (Rokka's Windermere, sessão de
# 2026-09-15 e 2026-09-24): a tela Worksheets, categoria 'Purchase Order',
# tem um filtro de Status próprio (`_SEL_STATUS_FILTER`, opções All/Pending/
# Awaiting Approval/Ordered/Received) ao lado da Categoria, e um campo de
# busca (`_SEL_SEARCH_FIELD`) cuja opção 'Supplier' busca pelo nome do
# fornecedor (não confundir com a opção 'Name', que é o nome do PRÓPRIO
# worksheet/PO, não do fornecedor). O PO de detalhe tem a aba Items com
# `Supplier Unit ID`, `Scancode`, `Receipt Alias`, `Item Name`, `Ordered`,
# `Received` e `Invoiced Total Cost` na MESMA linha (a `Receipt Alias` não
# precisa de ponte por `dim_item_catapult` — ver `commons/matcher.py::POLine`).
#
# Busca por fornecedor não isola mais um único PO como a busca por invoice
# fazia — pode voltar vários PO 'Ordered' do mesmo fornecedor, um por invoice
# em aberto. Quem chama (`crawler/flow/reconcile_erp_flow.py::_buscar_po`)
# desempata comparando os itens de cada candidato contra os da invoice
# (`conciliacao/reconcile_erp.py::escolher_po_por_itens`).
#
# A grade de itens não dá id por célula (é um "smart-grid" genérico); os ids
# abaixo (`gwt-debug-*`) são dos controles de busca/filtro, que esses sim têm
# id estável. A leitura da grade em si é posicional — ver `_JS_EXTRACT_ITEMS_GRID`.
# =============================================================================

_SEL_WORKSHEET_CATEGORY = "#gwt-debug-PreferenceListBox-worksheetTypes"
# Fica DESABILITADO (não aceita `select_option`, trava em timeout) enquanto
# 'Show History For' (abaixo) está marcado — confirmado contra o ambiente
# real, mesmo estilo visual acinzentado do dropdown de ano ao lado, que
# também trava nesse estado. Por isso `search_purchase_orders_by_supplier`
# sempre desmarca o histórico ANTES de mexer neste filtro.
_SEL_STATUS_FILTER = "#gwt-debug-PreferenceListBox-worksheetStatuses"
_SEL_SEARCH_FIELD = "#gwt-debug-EntityListBox-searchFields"
_SEL_SEARCH_MATCH_TYPE = "#gwt-debug-FilterSection-filterSection-searchbox-typeList"
_SEL_SEARCH_BOX = "#gwt-debug-FilterSection-filterSection-searchbox-textField"
_SEL_RESULT_LINKS = 'a.gwt-Anchor[href*="#purchaseOrder:pk="]'
_SEL_ITEMS_GRID = "#gwt-debug-WebOfficeFilterGrid-purchaseOrderGrid"

# 'Show History For: <ano>' — confirmado contra o ambiente real (Rokka's
# Windermere) que fica marcado ou desmarcado dependendo do estado deixado
# pela sessão/usuário anterior, não sempre marcado por padrão. Marcado, a
# grade mostra o HISTÓRICO (worksheets já Committed) só do ANO selecionado no
# dropdown ao lado; desmarcado, ignora ano e mostra só os worksheets ainda
# não-Committed (Pending/Ordered/Awaiting Approval), de qualquer data — que é
# exatamente o universo que a busca por fornecedor/Status=Ordered quer, então
# a busca sempre força DESMARCADO, sem restaurar o estado original ao final
# (deixou de ser um fallback pontual, é o comportamento padrão novo). O
# `<input>` tem `disabled=True` por uma fração de segundo bem no início da
# tela (antes do primeiro `select_option` de categoria terminar de
# redesenhar); a partir daí fica clicável. O clique precisa ser na LABEL, não
# no input: o input real fica coberto por ela (`intercepts pointer events`,
# confirmado — `.check()`/`.uncheck()` no input dão timeout).
_SEL_SHOW_HISTORY_CHECKBOX = "#gwt-debug-PreferenceCheckBox-showHistory-input"
_SEL_SHOW_HISTORY_LABEL = "#gwt-debug-PreferenceCheckBox-showHistory-label"

# Lê a grade por POSIÇÃO de coluna: o cabeçalho (`.sortable-column-spacing`)
# decide a ordem, e cada `td` do corpo (`table.smart-grid-body`) casa por
# índice. 'Received' aparece duas vezes no cabeçalho (quantidade recebida e o
# checkbox de conferência) — `!(h in obj)` fica só com a primeira, que é a
# quantidade (a que interessa pro motor de match).
_JS_EXTRACT_ITEMS_GRID = """
(grid) => {
  const headerDiv = grid.querySelector('.smart-grid-header');
  const headers = Array.from(headerDiv.querySelectorAll('.sortable-column-spacing'))
    .map(w => w.textContent.trim());
  const body = grid.querySelector('table.smart-grid-body');
  if (!body) return [];
  const rows = Array.from(body.querySelectorAll('tbody > tr'));
  function cellText(td) {
    const input = td.querySelector('input');
    return input ? input.value : td.textContent.trim();
  }
  return rows.map(tr => {
    const tds = Array.from(tr.children);
    const obj = {};
    tds.forEach((td, i) => {
      const h = headers[i];
      if (h && !(h in obj)) obj[h] = cellText(td);
    });
    return obj;
  });
}
"""


def open_worksheets(page, url: str, timeout: int = 30_000) -> None:
    """Vai pra tela Worksheets, de onde a busca de PO parte.

    Navega de novo pra `url` (a mesma do login) de propósito: o fragmento
    `#WorksheetEditor:` não sobrevive ao redirect do Cloudflare Access, então
    logo após `open_catapult_session` a página pode ter perdido a rota —
    navegar de novo, já autenticado, resolve (mesmo ajuste manual de
    `manutencao/catapult_explorar.py`, agora dentro da função de quem usa).
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        page.wait_for_selector(_SEL_WORKSHEET_CATEGORY, timeout=timeout)
    except PwError as exc:
        _debug_dump(page, "catapult_worksheets")
        raise IntegracaoException(
            f"tela Worksheets não abriu como esperado: {exc}"
        ) from exc


def search_purchase_orders_by_supplier(
    page, supplier_name: str, match_type: str = "Contains", timeout: int = 20_000,
    nomes_alternativos: tuple[str, ...] = (),
) -> list[dict]:
    """Busca PO pelo nome do fornecedor (filtro 'Supplier' da tela
    Worksheets, categoria 'Purchase Order', Status='Ordered', 'Show History
    For' sempre desmarcado — testado contra o ambiente real). Retorna uma
    linha por PO encontrado: `{'name': ..., 'href': ...}` — lista vazia é
    resultado normal (fornecedor sem PO 'Ordered' no momento), não erro.

    Pode vir mais de um PO pro mesmo fornecedor (um por invoice em aberto) —
    quem chama desempata pelos itens da invoice, ver
    `conciliacao/reconcile_erp.py::escolher_po_por_itens`.

    `match_type='Contains'` por padrão, não 'Begins with' (o padrão da
    tela): mesmo motivo já confirmado pra 'Invoice Reference' — nome digitado
    pode não ser um prefixo exato do nome cadastrado no Catapult.

    `nomes_alternativos`: outros nomes do mesmo fornecedor (ex. o nome cru da
    invoice quando `supplier_name` já é o termo do de-para `dim_fornecedor_alias`)
    aceitos na conferência do filtro — ver `_conferir_filtro_aplicado`.
    """
    _preparar_filtros_po(page, timeout)
    return _buscar_worksheets_por_fornecedor(
        page, supplier_name, match_type, timeout, nomes_alternativos,
    )


def _preparar_filtros_po(page, timeout: int) -> None:
    """Põe a tela Worksheets em categoria 'Purchase Order' + Status 'Ordered'.

    Separado de `search_purchase_orders_by_supplier` para o `try` caber numa
    função só (um `try` por função, sem aninhar) — aqui estão todas as
    interações que podem estourar timeout antes da busca em si.
    """
    # Desmarca o histórico ANTES de mexer no Status: o dropdown de Status
    # fica DESABILITADO enquanto 'Show History For' está marcado (confirmado
    # contra o ambiente real — mesmo estilo visual/bloqueio do dropdown de
    # ano ao lado). Fazer na ordem inversa trava em select_option esperando
    # o elemento ficar habilitado (timeout de 30s, nunca acontece).
    try:
        page.wait_for_selector(_SEL_WORKSHEET_CATEGORY, timeout=timeout)
        page.select_option(_SEL_WORKSHEET_CATEGORY, label="Purchase Order")
        page.wait_for_timeout(400)

        checkbox = page.locator(_SEL_SHOW_HISTORY_CHECKBOX)
        if checkbox.is_checked():
            page.locator(_SEL_SHOW_HISTORY_LABEL).click(timeout=timeout)
            page.wait_for_timeout(800)

        page.select_option(_SEL_STATUS_FILTER, label="Ordered")
        page.wait_for_timeout(400)
    except PwError as exc:
        _debug_dump(page, "catapult_filtros_po")
        raise IntegracaoException(
            f"filtros da tela Worksheets (categoria/Status) não responderam: {exc}"
        ) from exc


def _buscar_worksheets_por_fornecedor(
    page, supplier_name: str, match_type: str, timeout: int,
    nomes_alternativos: tuple[str, ...] = (),
) -> list[dict]:
    """Seleciona 'Supplier'/`match_type`, digita e lê a grade de resultados."""
    _aplicar_filtro_busca(page, "Supplier", supplier_name, match_type)

    try:
        page.wait_for_selector(_SEL_RESULT_LINKS, timeout=timeout)
    except PwTimeout:
        # Nenhum link na grade: fornecedor sem PO 'Ordered' no momento. Não
        # classifica nada porque não há falha nenhuma — é resultado de busca
        # vazio. Captura só `PwTimeout`, e o `PwError` abaixo garante que
        # erro de verdade do Playwright sobe classificado em vez de virar
        # "nenhum PO" em silêncio.
        return []
    except PwError as exc:
        _debug_dump(page, "catapult_grade_busca")
        raise IntegracaoException(
            f"grade de resultados da busca por {supplier_name!r} não respondeu: {exc}"
        ) from exc

    resultado = _ler_resultados(page)
    return _conferir_filtro_aplicado(page, resultado, supplier_name, match_type, nomes_alternativos)


def search_purchase_orders_by_invoice(
    page, invoice_number: str, match_type: str = "Equals", timeout: int = 20_000,
) -> list[dict]:
    """Busca PO pelo número da invoice (campo 'Invoice Reference', operador
    `match_type`), com os mesmos filtros da busca por fornecedor (categoria
    Purchase Order, Status='Ordered', histórico desmarcado). É a busca
    PRIORITÁRIA; a por nome (`search_purchase_orders_by_supplier`) é fallback.

    Lista vazia é resultado normal (nenhuma PO com essa referência), não erro.
    Com 'Equals' só vem PO cuja referência é exatamente este número; quem chama
    ainda confere os itens (`escolher_po_por_itens`) antes de gravar no PO.
    """
    _preparar_filtros_po(page, timeout)
    _aplicar_filtro_busca(page, "Invoice Reference", invoice_number, match_type)
    try:
        page.wait_for_selector(_SEL_RESULT_LINKS, timeout=timeout)
    except PwTimeout:
        return []
    except PwError as exc:
        _debug_dump(page, "catapult_grade_busca_invoice")
        raise IntegracaoException(
            f"grade de resultados da busca por invoice {invoice_number!r} não respondeu: {exc}"
        ) from exc
    return _ler_resultados(page)


def _aplicar_filtro_busca(page, campo: str, termo: str, match_type: str) -> None:
    """Escolhe o campo de busca (`campo`)/`match_type` e digita `termo`."""
    # A tela redesenha os controles de busca quando o campo (Name / Supplier
    # / Invoice Reference / ...) muda — confirmado contra o ambiente real que
    # agir rápido demais entre as trocas faz a busca sair IGNORADA em
    # silêncio: a grade volta pra listagem padrão sem filtro nenhum, em vez
    # de vazia ou do resultado esperado. As pausas dão tempo do redesenho
    # terminar.
    #
    # Digitação real (character a character) + Enter, não `.fill()` + clique
    # no botão — foi o que funcionou contra o ambiente real; `.fill()` seguido
    # de clique imediato no botão corre risco da mesma race condition acima.
    try:
        page.select_option(_SEL_SEARCH_FIELD, label=campo)
        page.wait_for_timeout(600)
        page.select_option(_SEL_SEARCH_MATCH_TYPE, label=match_type)
        page.wait_for_timeout(300)

        box = page.locator(_SEL_SEARCH_BOX)
        box.click()
        box.fill("")
        box.type(termo, delay=40)
        page.keyboard.press("Enter")
        page.wait_for_timeout(1500)  # a grade repopula de forma assincrona
    except PwError as exc:
        _debug_dump(page, "catapult_filtro_busca")
        raise IntegracaoException(
            f"campo de busca por {campo} não aceitou {termo!r}: {exc}"
        ) from exc


def _ler_resultados(page) -> list[dict]:
    """Lê nome e href de cada PO da grade de resultados."""
    try:
        links = page.locator(_SEL_RESULT_LINKS)
        return [
            {"name": links.nth(i).inner_text().strip(),
             "href": links.nth(i).get_attribute("href")}
            for i in range(links.count())
        ]
    except PwError as exc:
        _debug_dump(page, "catapult_grade_resultados")
        raise IntegracaoException(
            f"grade de resultados da busca de PO não pôde ser lida: {exc}"
        ) from exc


def extrair_prefixo_nome_po(name: str) -> str:
    """Parte do `Name` do PO antes do primeiro '-' ('Perdomo-036998-HQ-RS2'
    -> 'Perdomo'). Sem '-', devolve o nome inteiro."""
    return name.split("-", 1)[0].strip()


def _conferir_filtro_aplicado(
    page, resultado: list[dict], supplier_name: str, match_type: str,
    nomes_alternativos: tuple[str, ...] = (),
) -> list[dict]:
    """Devolve só os POs cujo prefixo do Name é confiavelmente o fornecedor
    buscado (fuzzy: `commons.matcher.aceitar_prefixos_catapult`). Levanta se
    nenhum for — a grade devolveu a listagem padrão em vez do resultado
    filtrado. Não toca no navegador — só confere o que já foi lido."""
    # Sanidade: se o filtro não pegou de verdade, a grade volta pra listagem
    # padrão (com Status=Ordered ainda aplicado, mas de TODOS os fornecedores
    # — nomes bem variados, sem nada em comum). Um limite de QUANTIDADE não
    # serve pra detectar isso (FreshPoint sozinho tem 199+ PO 'Ordered' num
    # store só). O sinal de verdade é o CONTEÚDO.
    #
    # O `Name` do PO NÃO é derivado do nome lido na invoice: é o nome que o
    # Catapult conhece ('Perdomo-036998-HQ-RS2' para a invoice 'Perdomo
    # Distributor'; 'Fresh Poin-008668-RS2' para 'Freshpoint Central FL'). Por
    # isso compara o PREFIXO do Name (antes do '-') por aproximação com o nome
    # buscado e, não achando, cai nos nomes alternativos (de-para `dim_fornecedor_alias`).
    if not resultado or not supplier_name.strip():
        return resultado
    prefixos = [extrair_prefixo_nome_po(r["name"]) for r in resultado]
    aceitos, scores = aceitar_prefixos_catapult((supplier_name, *nomes_alternativos), prefixos)
    if aceitos:
        return [r for r in resultado if extrair_prefixo_nome_po(r["name"]) in aceitos]
    log.warning(
        "busca por nome %r (alternativos: %s): nenhum prefixo confiavel entre %s PO(s); scores=%s",
        supplier_name, list(nomes_alternativos), len(resultado), scores,
    )
    _debug_dump(page, "catapult_filtro_ignorado")
    raise IntegracaoException(
        f"busca por Supplier {match_type!r}='{supplier_name}' devolveu "
        f"{len(resultado)} PO(s), nenhum com Name parecido com "
        f"{supplier_name!r} (alternativos: {list(nomes_alternativos)}) — parece que o filtro "
        f"não foi aplicado (a grade voltou pra listagem padrão sem filtro de fornecedor)."
    )


def open_purchase_order(page, href: str, timeout: int = 20_000) -> None:
    """Abre o PO pelo `href` devolvido por `search_purchase_orders_by_supplier`
    e espera a aba Items renderizar."""
    try:
        page.goto(href, wait_until="domcontentloaded", timeout=timeout)
        page.get_by_role("tab", name="Items", exact=True).click()
        page.wait_for_selector(_SEL_ITEMS_GRID, timeout=timeout)
    except PwError as exc:
        _debug_dump(page, "catapult_po_open")
        raise IntegracaoException(
            f"PO não abriu como esperado ({href}): {exc}"
        ) from exc


# Aba Receiving Information — seletores confirmados contra o Catapult real
# (Rokka's Dr Phillips, PO Fresh Poin-008668-RS2, sessão de 2026-09-16;
# Receive Date confirmado depois contra Rokka's Windermere, PO Mena
# Impor-034684-HQ-RS1, sessão de 2026-09-18 — `gwt-debug-PhoenixFieldValidator-
# receiveDate`, mesmo padrão de id dos outros dois). Os três campos usam id
# GWT estável, aceitam digitação direta e o formato é o mesmo `y-mm-dd` do
# placeholder — `date.isoformat()` bate exato, sem conversão. Digitar na data
# ABRE um calendário popup (GWT DatePicker); o botão Save é um componente
# customizado sem role de acessibilidade real — `get_by_role("button", ...)`
# nunca acha (0 matches, testado), por isso o seletor abaixo é por
# classe+texto.
_SEL_INVOICE_NUMBER = "#gwt-debug-PhoenixFieldValidator-invoiceNumber"
_SEL_INVOICE_DATE = "#gwt-debug-PhoenixFieldValidator-invoiceDate"
_SEL_RECEIVE_DATE = "#gwt-debug-PhoenixFieldValidator-receiveDate"
_SEL_SAVE_BUTTON = "button.icon-action-button:has-text('Save')"


def fill_receiving_invoice_info(
    page, invoice_number: str, invoice_date, timeout: int = 20_000,
) -> None:
    """Preenche Invoice Number/Invoice Date/Receive Date na aba Receiving
    Information do PO aberto (ver `open_purchase_order`) e salva.

    Sempre escreve os três campos com o que já foi lido da invoice, mesmo
    quando a conciliação vai acusar divergência de quantidade/valor — a
    identificação da nota fiscal (número, data) independe do resultado da
    comparação de itens. Por isso quem chama roda isto ANTES de
    `scrape_po_items`/`reconcile_items_against_po`, não depois.

    Receive Date recebe a MESMA data da invoice (não a data em que o RPA
    processou) — regra de negócio: a loja recebe a mercadoria na data que a
    nota fiscal traz, o Catapult não tem um "data de recebimento real"
    separado nesse fluxo.

    PO já `Committed` deixa os três campos `disabled` (não dá mais para
    editar depois de aprovado) — trata como no-op nesse caso, não erro: não
    há nada errado, só não há mais o que preencher.

    Deixa a página de volta na aba Items ao final, para `scrape_po_items`
    continuar funcionando sem saber que esta função existiu.

    CONFIRMADO contra o Catapult real (PO Fresh Poin-008668-RS2, invoice
    FreshPoint 1294081311 de 2026-09-14): Invoice Number/Date preenchidos,
    Save clicado e persistido — reaberto o PO depois e os campos vieram com
    o valor salvo.
    """
    _abrir_aba_receiving(page, timeout)

    campo_numero = page.locator(_SEL_INVOICE_NUMBER)
    if _esta_desabilitado(page, campo_numero, "invoiceNumber"):
        log.info("catapult: PO ja Committed - Invoice Number/Date/Receive Date "
                 "nao editaveis, pulando")
    else:
        _preencher_e_salvar(page, campo_numero, invoice_number, invoice_date, timeout)

    _voltar_para_items(page, timeout)


def _abrir_aba_receiving(page, timeout: int) -> None:
    """Vai para a aba Receiving Information do PO aberto."""
    try:
        page.get_by_role("tab", name="Receiving Information", exact=True).click()
        page.wait_for_selector(_SEL_INVOICE_NUMBER, timeout=timeout)
    except PwError as exc:
        _debug_dump(page, "catapult_receiving_tab")
        raise IntegracaoException(
            f"aba Receiving Information não abriu: {exc}"
        ) from exc


def _preencher_e_salvar(page, campo_numero, invoice_number, invoice_date, timeout: int) -> None:
    """Escreve número/data da nota e a data de recebimento, e salva."""
    try:
        campo_numero.fill(str(invoice_number))
        page.locator(_SEL_INVOICE_DATE).fill(invoice_date.isoformat())
        # Tab (nao Escape) pra sair do campo de data: Escape CANCELA a
        # edicao do GWT DatePicker (reverte o valor, Save fica desabilitado
        # de novo) — confirmado contra o ambiente real. Tab fecha o popup
        # e COMMITA o valor digitado, do jeito que o Save espera. Precisa
        # de um Tab por campo de data (cada um abre seu proprio popup).
        page.keyboard.press("Tab")
        page.locator(_SEL_RECEIVE_DATE).fill(invoice_date.isoformat())
        page.keyboard.press("Tab")
        save_btn = page.locator(_SEL_SAVE_BUTTON)
        # Dentro do try desta função de propósito: aqui um PwError já sai
        # classificado como "salvar ... falhou", que é o contexto certo.
        if save_btn.is_disabled():
            # PO já tinha exatamente estes valores (reprocesso da mesma
            # nota) — nada mudou, o Save fica desabilitado sem erro
            # nenhum. Confirmado contra o ambiente real.
            log.info("catapult: Invoice Number/Date/Receive Date ja estavam com "
                     "este valor, nada para salvar")
        else:
            save_btn.click()
            page.wait_for_load_state("networkidle", timeout=timeout)
    except PwError as exc:
        _debug_dump(page, "catapult_receiving_save")
        raise IntegracaoException(
            f"salvar Invoice Number/Date/Receive Date do PO falhou: {exc}"
        ) from exc


def _voltar_para_items(page, timeout: int) -> None:
    """Deixa a página de volta na aba Items, como `scrape_po_items` espera."""
    try:
        page.get_by_role("tab", name="Items", exact=True).click()
        page.wait_for_selector(_SEL_ITEMS_GRID, timeout=timeout)
    except PwError as exc:
        _debug_dump(page, "catapult_receiving_back_to_items")
        raise IntegracaoException(
            f"não voltou pra aba Items após salvar: {exc}"
        ) from exc


def _esta_desabilitado(page, campo, nome: str) -> bool:
    """`campo.is_disabled()` com a falha classificada.

    Vive à parte porque `fill_receiving_invoice_info` precisa desta pergunta
    FORA dos `try` dela (é o `if` que escolhe entre preencher e pular), e
    solta ela escapava como `PwError` cru.
    """
    try:
        return campo.is_disabled()
    except PwError as exc:
        _debug_dump(page, "catapult_campo_disabled")
        raise IntegracaoException(
            f"não foi possível checar se o campo {nome} do PO está editável: {exc}"
        ) from exc


def _extrair_grade_itens(page) -> list[dict]:
    """Roda o JS de leitura da grade Items, com a falha classificada."""
    try:
        return page.eval_on_selector(_SEL_ITEMS_GRID, _JS_EXTRACT_ITEMS_GRID)
    except PwError as exc:
        _debug_dump(page, "catapult_extrair_grade")
        raise IntegracaoException(
            f"leitura da grade de itens do PO falhou: {exc}"
        ) from exc


def scrape_po_items(page, timeout: int = 20_000) -> list[dict]:
    """Lê a grade Items do PO aberto (ver `open_purchase_order`).

    Retorna uma linha crua por item, com as chaves do cabeçalho tal como
    aparecem na tela ('Supplier Unit ID', 'Scancode', 'Receipt Alias',
    'Item Name', 'Ordered', 'Received', 'Invoiced Total Cost', ...). Use
    `to_po_lines` pra converter pro contrato do motor de match.
    """
    _esperar_grade(page, timeout)
    _esperar_corpo_da_grade(page, timeout)
    return _extrair_grade_itens(page)


def _esperar_grade(page, timeout: int) -> None:
    """Espera o container da grade Items aparecer."""
    try:
        page.wait_for_selector(_SEL_ITEMS_GRID, timeout=timeout)
    except PwError as exc:
        _debug_dump(page, "catapult_po_items")
        raise IntegracaoException(
            f"grade de itens do PO não carregou: {exc}"
        ) from exc


def _esperar_corpo_da_grade(page, timeout: int) -> None:
    """Espera a primeira linha do corpo da grade. Timeout aqui NÃO é erro."""
    # A grade carrega em duas fases: o container aparece primeiro (é o que
    # `_esperar_grade` confirma), o CORPO com as linhas vem depois, numa
    # chamada assíncrona separada — confirmado contra o ambiente real: ler
    # logo após o container aparecer pegou a grade ainda vazia (0 itens) num
    # PO que tinha 3. PO genuinamente sem item (raro) só cai no timeout e
    # segue com a lista vazia mesmo — não é erro, por isso não levanta.
    try:
        page.wait_for_selector(
            f"{_SEL_ITEMS_GRID} table.smart-grid-body tbody tr", timeout=timeout,
        )
    except PwTimeout:
        pass
    except PwError as exc:
        # Timeout é normal (PO sem item); qualquer OUTRO erro do Playwright
        # não é, e não pode virar "grade vazia" em silêncio.
        _debug_dump(page, "catapult_po_items_corpo")
        raise IntegracaoException(
            f"corpo da grade de itens do PO não pôde ser esperado: {exc}"
        ) from exc


def _parse_money(text: str | None) -> Decimal | None:
    """'$14.50' -> Decimal('14.50'); vazio/None -> None."""
    if not text:
        return None
    cleaned = text.replace("$", "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _parse_qty(text: str | None) -> Decimal | None:
    """'5.000' -> Decimal('5.000'); vazio/None -> None."""
    if not text:
        return None
    cleaned = text.replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def to_po_lines(raw_rows: list[dict]) -> list[POLine]:
    """Converte as linhas cruas de `scrape_po_items` pro contrato do motor de
    match (`commons.matcher.POLine`).

    A chave é a posição na lista — o Catapult não expõe um id de linha
    estável na grade, e não precisa: quem grava o resultado da conciliação
    usa `id_invoice_item` (lado da invoice), não um id do lado do PO.
    """
    return [
        POLine(
            key=i,
            supplier_unit_id=row.get("Supplier Unit ID") or None,
            scancode=row.get("Scancode") or None,
            item_name=row.get("Item Name") or None,
            ordered=_parse_qty(row.get("Ordered")),
            received=_parse_qty(row.get("Received")),
            invoiced_total_cost=_parse_money(row.get("Invoiced Total Cost")),
            receipt_alias=row.get("Receipt Alias") or None,
            unit=row.get("Unit") or None,
        )
        for i, row in enumerate(raw_rows)
    ]

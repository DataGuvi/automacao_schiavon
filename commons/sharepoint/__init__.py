"""Navegação SharePoint via REST API e automação de browser com Playwright."""

from __future__ import annotations

import json
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, quote as urlquote, unquote, urlparse
from playwright.sync_api import TimeoutError as PwTimeout
from commons.datas import week_bounds
from commons.logging_config import get_logger

log = get_logger(__name__)

#_LIMITE_DOWNLOAD_TESTE: int | None

# Viewport largo: em 1280x720 (padrao do headless) a command bar do SharePoint
# colapsa no menu "...", e os seletores dos botoes individuais deixam de achar.
VIEWPORT = {"width": 1920, "height": 945}


def _novo_contexto(browser):
    """Contexto do SharePoint: viewport fixo e downloads habilitados."""
    return browser.new_context(
        viewport=VIEWPORT, accept_downloads=True,
        locale="pt-BR", timezone_id="America/Sao_Paulo",
    )


def _lancar_chromium(pw, headless: bool):
    """Headless usa o Chromium completo (channel chromium), como o Catapult: o
    headless-shell padrao e mais facil de a Microsoft tratar como bot."""
    return pw.chromium.launch(headless=headless, channel="chromium" if headless else None)


# Step: nome de pasta (str) ou função que recebe a listagem e retorna a pasta
Step = str | Callable[[list[dict]], dict | None]

_PT_MES = {
    1: "JAN", 2: "FEV", 3: "MAR", 4: "ABR",
    5: "MAI", 6: "JUN", 7: "JUL", 8: "AGO",
    9: "SET", 10: "OUT", 11: "NOV", 12: "DEZ",
}

# O cliente as vezes nomeia o mes em ingles ("02 FEB - 2026" na Windermere):
# `resolve_month_folder` aceita as duas abreviacoes (spec-coleta-arquivos-soltos R16).
_EN_MES = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR",
    5: "MAY", 6: "JUN", 7: "JUL", 8: "AUG",
    9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}


# ---------------------------------------------------------------------------
# Helpers de data / nome de pasta
# ---------------------------------------------------------------------------

def last_week_reference(reference: date | None = None) -> date:
    """Retorna a data de 7 dias antes da referência (padrão: hoje).

    Usada para navegar até a pasta da semana passada em vez da corrente —
    o SharePoint só termina de consolidar os arquivos da semana alguns dias
    depois dela fechar.
    """
    return (reference or date.today()) - timedelta(days=7)


def current_year_folder(reference: date | None = None) -> str:
    """Retorna o ano da data de referência como string (ex: '2026'). Padrão: hoje."""
    return str((reference or date.today()).year)


def current_month_folder(reference: date | None = None) -> str:
    """Retorna o nome da pasta do mês da data de referência (ex: '06 JUN - 2026'). Padrão: hoje."""
    ref = reference or date.today()
    return f"{ref.month:02d} {_PT_MES[ref.month]} - {ref.year}"


def resolve_invoices_launch_folder(entries: list[dict]) -> dict | None:
    """Procura por uma pasta de lançamento de invoices com variações de nome."""
    target = "_invoices para lançamento"
    normalized = target.lower().replace(" ", "")
    for entry in entries:
        if entry["type"] != "pasta":
            continue
        name = entry["name"].strip().lower().replace(" ", "")
        if name == normalized or "invoicesparalancamento" in name or "invoices para lançamento" in name:
            return entry
    return None


def resolve_month_folder(entries: list[dict], reference: date | None = None) -> dict | None:
    """Procura o mês da data de referência em pastas com variações de formato. Padrão: hoje."""
    ref = reference or date.today()
    expected = current_month_folder(ref).lower()
    abreviacoes = (_PT_MES[ref.month].lower(), _EN_MES[ref.month].lower())
    for entry in entries:
        if entry["type"] != "pasta":
            continue
        name = entry["name"].strip().lower()
        if name == expected or f"{ref.month:02d}" in name and any(a in name for a in abreviacoes):
            return entry
    return None


def semanas_da_coleta(hoje: date | None = None) -> list[date]:
    """Segundas-feiras das semanas varridas pela coleta: a anterior e a atual
    (spec-coleta-arquivos-soltos R4/R9)."""
    segunda, _ = week_bounds(hoje or date.today())
    return [segunda - timedelta(days=7), segunda]


def nome_pasta_semana(reference: date) -> str:
    """Nome que o cliente da a pasta da semana: 'DD A DD', da segunda ao
    domingo (ex.: '28 A 04' para 28/09 a 04/10)."""
    segunda, domingo = week_bounds(reference)
    return f"{segunda.day:02d} A {domingo.day:02d}"


def _match_week_folder(entries: list[dict], dia_inicio: int) -> dict | None:
    """Procura a pasta 'DD A DD' que COMECA no dia informado (a segunda-feira).

    Casa pelo inicio, nao pela faixa: a semana que vira o mes ('28 A 04') tem
    inicio maior que o fim e nunca caberia em `inicio <= dia <= fim`.
    """
    for entry in entries:
        if entry["type"] != "pasta":
            continue
        name = entry["name"].strip()
        m = re.match(r"^(\d{1,2})\s*[Aa-]\s*(\d{1,2})$", name)
        if m and int(m.group(1)) == dia_inicio:
            return entry
    return None


def resolve_week_folder(entries: list[dict], reference: date | None = None) -> dict | None:
    """
    Recebe a listagem de pastas do mês e retorna a da semana da data de
    referência (padrão: hoje): a pasta 'DD A DD' que começa na segunda-feira
    dessa semana. Também aceita variações como '28 a 04' e '28-04'.

    Sem fallback para a semana anterior: a coleta já varre as duas semanas
    em toda execução (spec-coleta-arquivos-soltos R7).
    """
    segunda, _ = week_bounds(reference or date.today())
    return _match_week_folder(entries, segunda.day)


def build_nav_steps(reference: date | None = None) -> list[Step]:
    """
    Define o caminho de navegação dentro de cada SharePoint.
    `reference` controla ano/mês/semana buscados (padrão: hoje).
    Ajuste aqui quando a estrutura de pastas mudar.

    Ano e mês saem da SEGUNDA-FEIRA da semana: o cliente guarda a semana que
    vira o mês no mês em que ela começa (ex.: '09 SET - 2026/28 A 04').
    """
    segunda, _ = week_bounds(reference or date.today())
    return [
        "Invoices Fornecedores",
        current_year_folder(segunda),
        _passo(lambda entries: resolve_invoices_launch_folder(entries),
               "_Invoices para lançamento"),
        _passo(lambda entries: resolve_month_folder(entries, segunda), current_month_folder(segunda)),
        _passo(lambda entries: resolve_week_folder(entries, segunda), nome_pasta_semana(segunda)),
    ]


def _passo(fn: Callable[[list[dict]], dict | None], esperado: str):
    """Anota no passo-funcao o nome da pasta que ele procura, para o log de
    "nao encontrada" dizer O QUE faltou."""
    fn.esperado = esperado
    return fn


# ---------------------------------------------------------------------------
# SharePoint REST API
# ---------------------------------------------------------------------------

def parse_sharepoint_url(url: str) -> tuple[str, str | None]:
    """
    Extrai (site_url, folder_server_relative_path) de uma URL SharePoint
    no formato AllItems.aspx?id=...
    """
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.split("/") if p]
    if len(path_parts) >= 2 and path_parts[0].lower() == "sites":
        site_path = f"/sites/{path_parts[1]}"
    else:
        site_path = "/" + path_parts[0] if path_parts else "/"
    site_url = f"{parsed.scheme}://{parsed.netloc}{site_path}"
    qs = parse_qs(parsed.query)
    id_param = qs.get("id", [None])[0]
    return site_url, unquote(id_param) if id_param else None


def _odata_escape(value: str) -> str:
    """Dobra apóstrofo para caber num literal OData `'...'`.

    Nome de fornecedor/arquivo com apóstrofo (ex.: "Kelly's Foods") fecha a
    string no meio se o apóstrofo não for escapado — o SharePoint responde
    "Url ausente ou inválida" em vez de um erro sobre aspas.
    """
    return value.replace("'", "''")


def get_folder_contents(context, site_url: str, folder_path: str, base: str) -> list[dict]:
    """
    Chama a REST API do SharePoint e retorna pastas e arquivos de um caminho.
    Usa a sessão autenticada do contexto Playwright.
    """
    api_url = (
        f"{site_url}/_api/web"
        f"/GetFolderByServerRelativePath(decodedurl=@p)"
        f"?@p='{_odata_escape(folder_path)}'"
        f"&$expand=Folders,Files"
        f"&$select="
        f"Folders/Name,Folders/ServerRelativeUrl,Folders/ItemCount,"
        f"Files/Name,Files/ServerRelativeUrl,Files/Length,Files/TimeLastModified"
    )
    resp = context.request.get(api_url, headers={"Accept": "application/json;odata=verbose"})
    if not resp.ok:
        raise RuntimeError(f"Erro {resp.status} ao acessar '{folder_path}': {resp.text()[:300]}")

    root = resp.json().get("d", {})
    entries: list[dict] = []

    for folder in root.get("Folders", {}).get("results", []):
        if folder.get("Name") == "Forms":
            continue
        entries.append({
            "name": folder["Name"],
            "type": "pasta",
            "item_count": folder.get("ItemCount"),
            "web_url": base + folder["ServerRelativeUrl"],
            "server_relative_url": folder["ServerRelativeUrl"],
        })

    for file in root.get("Files", {}).get("results", []):
        entries.append({
            "name": file["Name"],
            "type": "arquivo",
            "size_bytes": int(file.get("Length") or 0),
            "last_modified": file.get("TimeLastModified"),
            "web_url": base + file["ServerRelativeUrl"],
            "server_relative_url": file["ServerRelativeUrl"],
        })

    return entries


def navigate(context, site_url: str, base: str, root_path: str, steps: list[Step]) -> tuple[str, list[dict]]:
    """
    Percorre a hierarquia de pastas passo a passo.
    Cada step pode ser str (nome exato, case-insensitive) ou callable(entries) → dict.
    Retorna (caminho_final, listagem_final).
    """
    current_path = root_path
    for step in steps:
        entries = get_folder_contents(context, site_url, current_path, base)
        if callable(step):
            match = step(entries)
            esperado = getattr(step, "esperado", "pasta esperada")
        else:
            match = next((e for e in entries if e["name"].lower() == step.lower()), None)
            esperado = step

        if match is None:
            log.info("-> %s (nao encontrada)", esperado)
            raise RuntimeError(f"Pasta '{esperado}' nao encontrada em '{current_path}'.")
        log.info("-> %s", match["name"])
        current_path = match["server_relative_url"]

    return current_path, get_folder_contents(context, site_url, current_path, base)


# ---------------------------------------------------------------------------
# Download e renomeação de arquivos
# ---------------------------------------------------------------------------

_PREFIX_MAP = {
    "windermere": "wind",
    "phillips":   "drphil",
}


def _get_prefix(record: dict) -> str:
    """
    Deriva o prefixo a partir da URL do config (mais confiável que o name).
    A URL contém o nome do site SharePoint: Scanner-Windermere / Scanner-Dr.Phillips.
    """
    # Busca na URL e no name (case-insensitive)
    search = " ".join([
        (record.get("url") or ""),
        (record.get("name") or ""),
        (record.get("description") or ""),
    ]).lower()

    for keyword, prefix in _PREFIX_MAP.items():
        if keyword in search:
            return prefix

    # Fallback: primeiros 4 chars do name sem hifens
    return (record.get("name") or "file")[:4].lower().replace("-", "")


def _build_filename(prefix: str, original_name: str) -> str:
    """
    Monta o nome do arquivo de destino.
    Formato: {prefix}_{stem}_{dd-mm-yyyy}{extensão}
    """
    today = date.today()
    p = Path(original_name)
    return f"{prefix}_{p.stem}_{today.strftime('%d-%m-%Y')}{p.suffix}"


def _already_downloaded(original_name: str, *search_dirs: Path) -> Path | None:
    """
    Verifica se o arquivo original do SharePoint já foi baixado anteriormente.
    Busca pelo stem do nome original dentro dos diretórios informados.
    Recursivo porque read_files/ agora agrupa os arquivos em subpastas por dia
    de leitura (invoice_DD-MM-AAAA/).
    Retorna o caminho encontrado ou None.
    """
    stem = Path(original_name).stem.lower()
    for directory in search_dirs:
        if not directory.exists():
            continue
        for existing in directory.rglob("*"):
            if existing.is_file() and stem in existing.name.lower():
                return existing
    return None


def download_files(
    context,
    entries: list[dict],
    record: dict,
    dest_dir: Path,
    skip_dirs: list[Path] | None = None,
) -> list[Path]:
    """
    Baixa todos os arquivos (type=='arquivo') listados em entries.
    Renomeia com o prefixo do config e a data de hoje.

    skip_dirs: pastas adicionais verificadas para evitar re-download
               (ex: read_files/). Se o nome original já existir em qualquer
               uma delas, o arquivo é ignorado.
    """
    prefix = _get_prefix(record)
    dest_dir.mkdir(parents=True, exist_ok=True)
    check_dirs = [dest_dir] + (skip_dirs or [])
    downloaded: list[Path] = []

    global _LIMITE_DOWNLOAD_TESTE

    files_only = [e for e in entries if e["type"] == "arquivo"]
    if not files_only:
        log.info("Nenhum arquivo para baixar.")
        return downloaded

    # if _LIMITE_DOWNLOAD_TESTE is not None:
    #     files_only = files_only[:max(_LIMITE_DOWNLOAD_TESTE, 0)]

    for entry in files_only:
        original_name = entry["name"]

        # Verifica se o arquivo já existe (pelo nome original do SharePoint)
        existing = _already_downloaded(original_name, *check_dirs)
        if existing:
            log.info("ja existe: %s -> %s (ignorado)", original_name, existing.name)
            continue

        dest_name = _build_filename(prefix, original_name)
        dest_path = dest_dir / dest_name
        log.info("Baixando : %s -> %s", original_name, dest_name)

        parsed = urlparse(entry["web_url"])
        path_parts = [p for p in parsed.path.split("/") if p]
        site_url = f"{parsed.scheme}://{parsed.netloc}/sites/{path_parts[1]}"
        download_url = (
            f"{site_url}/_api/web"
            f"/GetFileByServerRelativePath(decodedurl=@p)/$value"
            f"?@p='{_odata_escape(entry['server_relative_url'])}'"
        )

        try:
            resp = context.request.get(download_url)
            content_type = resp.headers.get("content-type", "")
            if resp.ok and "text/html" not in content_type:
                dest_path.write_bytes(resp.body())
                downloaded.append(dest_path)
                size_kb = dest_path.stat().st_size / 1024
                log.info("%.1f KB", size_kb)
            else:
                log.error("status=%s content-type=%s", resp.status, content_type[:80])
        except Exception as exc:
            log.error("Falha: %s", exc)

    # if _LIMITE_DOWNLOAD_TESTE is not None:
    #     _LIMITE_DOWNLOAD_TESTE -= len(downloaded)

    return downloaded


# ---------------------------------------------------------------------------
# Browser Playwright
# ---------------------------------------------------------------------------

class SharePointLoginError(RuntimeError):
    """Falha de autenticação no portal Microsoft (credencial, MFA, bloqueio).

    Distinta de erro de navegação (pasta não encontrada): quem trata separa as
    duas para gravar `ERRO_LOGIN` e alertar a operação sobre QUAL sistema caiu.
    """


def _handle_microsoft_login(page, username: str, password: str) -> None:
    """Preenche o login Microsoft se a página redirecionar para o portal de auth."""
    from playwright.sync_api import TimeoutError as PwTimeout

    if "login.microsoftonline.com" not in page.url:
        return

    if not (username or "").strip() or not (password or "").strip():
        raise SharePointLoginError(
            "credencial do SharePoint vazia: confira USER_GUVI e PASSWORD_GUVI no profile")

    log.info("[login] Autenticando no Microsoft...")
    try:
        page.wait_for_selector('input[name="loginfmt"]', timeout=15_000)
        _enviar_usuario(page, username)

        _aguardar_tela_senha(page, username)
        _preencher_campo(page, 'input[name="passwd"]', password)
        page.click('input[type="submit"]')

        # Espera um DESFECHO real do envio da senha: saiu do portal, erro de
        # senha ou o prompt "continuar conectado". Esperar tambem por
        # `loginfmt`/`#usernameError` (que ficam no DOM da tela de senha)
        # fazia a espera voltar na hora, antes de o portal processar o envio.
        if not _aguardar_desfecho(page, 10_000):
            # Sem resposta (visto no servidor): o portal pode ter limpado o campo.
            # Nunca reenvia as cegas: um Enter com o campo vazio gera "Digite sua senha".
            log.warning("[login] senha enviada sem resposta em 10s; conferindo o campo")
            _logar_campos_login(page)
            _debug_dump(page, "sem_resposta")
            _preencher_campo(page, 'input[name="passwd"]', password)
            page.click('input[type="submit"]')
            page.wait_for_function(_JS_DESFECHO_LOGIN, timeout=30_000)

    except PwTimeout as exc:
        _logar_campos_login(page)
        recusa = _texto_erro_visivel(page, "#usernameError")
        _debug_dump(page, "login_recusado" if recusa else "timeout_pos_submit")
        if recusa:
            raise SharePointLoginError(f"login recusado: {recusa}") from exc
        raise SharePointLoginError(
            f"portal de login não respondeu como esperado: {exc}") from exc

    recusa = _texto_erro_visivel(page, "#passwordError")
    if recusa:
        _logar_campos_login(page)
        _debug_dump(page, "login_recusado")
        raise SharePointLoginError(f"login recusado: {recusa}")

    _dismiss_kmsi_prompt(page)
    _confirmar_saida_do_portal(page)

    log.info("[login] Concluido.")


# Desfecho do envio da senha: saiu do portal, ou erro de senha, ou prompt KMSI.
_JS_DESFECHO_LOGIN = """() => {
    const visivel = (sel) => {
        const el = document.querySelector(sel);
        return !!el && el.offsetParent !== null;
    };
    return !location.host.includes('login.microsoftonline.com')
        || visivel('#passwordError') || visivel('#idBtn_Back');
}"""


# Tela de senha de verdade: mostra o e-mail do usuario em #displayName. O input
# `passwd` ja conta como visivel na tela do usuario, entao esperar so por ele deixa
# o robo preencher/clicar antes da transicao (servidor lento) e o portal recebe o
# formulario sem usuario (AADSTS90100: login parameter is empty).
_JS_TELA_SENHA = """() => {
    const nome = document.querySelector('#displayName');
    const senha = document.querySelector('input[name="passwd"]');
    return !!nome && nome.innerText.trim().length > 0
        && !!senha && senha.offsetParent !== null;
}"""


def _aguardar_tela_senha(page, username: str) -> None:
    """Espera a tela de senha ativa; reenvia o usuario uma vez e, se nao vier, falha.

    Seguir para a senha na tela do usuario faz o portal receber o formulario sem
    usuario (AADSTS90100), o que esconde a causa real.
    """
    page.wait_for_selector('input[name="passwd"]', timeout=15_000)
    if not _tela_senha_ativa(page, 30_000):
        log.warning("[login] tela de senha sem #displayName em 30s; reenviando o usuario")
        _logar_campos_login(page)
        # Com o campo vazio, o "e-mail invalido" e do proprio robo (a pagina
        # apagou o valor), nao recusa da conta: reenvia em vez de desistir.
        recusa = ""
        if _valor_campo(page, "loginfmt"):
            recusa = _texto_erro_visivel(page, "#usernameError")
        if recusa:
            _debug_dump(page, "login_recusado")
            raise SharePointLoginError(f"login recusado: {recusa}")
        _enviar_usuario(page, username, digitar=True)
        if not _tela_senha_ativa(page, 30_000):
            _logar_campos_login(page)
            _debug_dump(page, "tela_senha")
            raise SharePointLoginError(
                "portal nao avancou para a tela de senha apos enviar o usuario duas vezes")
    page.wait_for_timeout(500)


def _tela_senha_ativa(page, timeout_ms: int) -> bool:
    """True se a tela de senha ficou ativa dentro do prazo; False se estourou."""
    from playwright.sync_api import TimeoutError as PwTimeout

    try:
        page.wait_for_function(_JS_TELA_SENHA, timeout=timeout_ms)
    except PwTimeout:
        return False
    return True


def _aguardar_desfecho(page, timeout_ms: int) -> bool:
    """True se o login chegou a um desfecho dentro do prazo; False se estourou."""
    from playwright.sync_api import TimeoutError as PwTimeout

    try:
        page.wait_for_function(_JS_DESFECHO_LOGIN, timeout=timeout_ms)
    except PwTimeout:
        return False
    return True


def _logar_campos_login(page) -> None:
    """Diagnostico sem segredo: so o TAMANHO do que ficou nos campos de login."""
    for nome in ("loginfmt", "passwd"):
        campo = page.query_selector(f'input[name="{nome}"]')
        if campo:
            log.info("[debug] campo %s: visivel=%s tamanho_do_valor=%s",
                     nome, campo.is_visible(), len(campo.input_value()))


def _valor_campo(page, nome: str) -> str:
    """Valor atual de um campo do login; vazio se o campo nao existe."""
    campo = page.query_selector(f'input[name="{nome}"]')
    return campo.input_value() if campo else ""


def _texto_erro_visivel(page, seletor: str) -> str:
    """Texto do elemento de erro se ele esta visivel; vazio caso contrario."""
    el = page.query_selector(seletor)
    if el and el.is_visible():
        return el.inner_text()[:200]
    return ""


def _preencher_campo(page, seletor: str, valor: str) -> None:
    """Preenche um campo do login e confere que o valor ficou la.

    Em headless/Linux o `fill` as vezes nao fica registrado pela pagina da
    Microsoft. Se o valor lido nao bate, limpa e digita tecla a tecla.
    """
    campo = page.locator(seletor)
    campo.fill(valor)
    if campo.input_value() != valor:
        log.warning("[login] campo %s nao registrou o fill; digitando tecla a tecla", seletor)
        campo.fill("")
        campo.press_sequentially(valor, delay=40)


def _preencher_usuario(page, username: str) -> None:
    _preencher_campo(page, 'input[name="loginfmt"]', username)


def _enviar_usuario(page, username: str, digitar: bool = False) -> None:
    """Preenche o e-mail e clica em "Avancar".

    Espera o botao visivel antes: com o servidor lento, preencher antes de a
    pagina terminar de montar faz o valor nao chegar ao formulario. `digitar`
    forca tecla a tecla (reenvio), que dispara os eventos que a pagina escuta.
    """
    page.wait_for_selector("#idSIButton9", state="visible", timeout=15_000)
    _aguardar_pagina_estavel(page)
    campo = page.locator('input[name="loginfmt"]')
    if digitar:
        campo.fill("")
        campo.press_sequentially(username, delay=40)
    else:
        _preencher_usuario(page, username)
    # Visto no servidor: o valor passa na conferencia do fill e a pagina o apaga
    # logo depois, ao terminar de montar. Reconfere imediatamente antes do clique.
    page.wait_for_timeout(1_000)
    if campo.input_value() != username:
        log.warning("[login] campo de usuario foi limpo pela pagina; digitando de novo")
        campo.fill("")
        campo.press_sequentially(username, delay=40)
    page.click("#idSIButton9")


def _aguardar_pagina_estavel(page) -> None:
    """Espera a rede do portal assentar (scripts que remontam o formulario).
    Nunca levanta: o portal tem telemetria que pode nao deixar a rede parar."""
    from playwright.sync_api import TimeoutError as PwTimeout

    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PwTimeout:
        log.debug("[login] rede do portal nao assentou em 10s; seguindo")


def _confirmar_saida_do_portal(page) -> None:
    """Espera a URL deixar o portal Microsoft. Função à parte para o `try`
    caber uma vez só por função (governança)."""
    from playwright.sync_api import TimeoutError as PwTimeout

    if "login.microsoftonline.com" not in page.url:
        return
    try:
        page.wait_for_url(
            lambda url: "login.microsoftonline.com" not in url,
            timeout=15_000,
        )
    except PwTimeout as exc:
        _debug_dump(page, "timeout_pos_kmsi")
        raise SharePointLoginError(
            f"ainda no portal de login após submeter as credenciais "
            f"(url atual: {page.url})"
        ) from exc


def _dismiss_kmsi_prompt(page) -> None:
    from playwright.sync_api import TimeoutError as PwTimeout

    try:
        btn = page.locator('xpath=//input[@id="idBtn_Back"]')
        btn.wait_for(state="visible", timeout=12_000)
        btn.click()
        page.wait_for_url(
            lambda url: "login.microsoftonline.com" not in url,
            timeout=15_000,
        )
    except PwTimeout:
        _debug_dump(page, "timeout_kmsi_dismiss")


def _debug_dump(page, tag: str) -> None:
    """Salva screenshot + url + html no momento da falha, pra diagnóstico."""
    import time
    ts = int(time.time())
    try:
        page.screenshot(path=f"debug_{tag}_{ts}.png", full_page=True)
        with open(f"debug_{tag}_{ts}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        log.info("[debug] url no momento da falha: %s", page.url)
        # Texto visivel da pagina: permite diagnosticar num servidor so de terminal.
        texto = " ".join(page.inner_text("body").split())
        log.info("[debug] texto da pagina: %s", texto[:600])
        log.info("[debug] screenshot salvo em debug_%s_%s.png", tag, ts)
    except Exception as e:
        log.error("[debug] falhou ao salvar dump: %s", e)


def _navigate_browser_to(page, base: str, final_path: str) -> None:
    """Navega o browser visualmente até a pasta final no SharePoint."""
    path_parts = [p for p in final_path.split("/") if p]
    library_root = "/" + "/".join(path_parts[:3])
    url = f"{base}{library_root}/Forms/AllItems.aspx?id={urlquote(final_path)}"
    page.goto(url, wait_until="networkidle", timeout=30_000)


def _process_record(
    context,
    page,
    record: dict,
    nav_steps: list[Step],
    credentials: tuple[str, str],
    dest_dir: Path | None = None,
    skip_dirs: list[Path] | None = None,
) -> tuple[str | None, list[dict], list[Path]]:
    """
    Dentro de uma sessão de browser já aberta, acessa a URL do registro,
    faz login se necessário, navega até a pasta alvo e baixa os arquivos.
    """
    url = record.get("url")
    if not url:
        raise RuntimeError("URL não configurada no registro.")

    site_url, root_folder_path = parse_sharepoint_url(url)
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    if not root_folder_path:
        raise RuntimeError("Não foi possível extrair o caminho da pasta da URL.")

    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    _handle_microsoft_login(page, *credentials)
    page.wait_for_url(f"**{parsed.netloc}**", timeout=30_000)
    page.wait_for_load_state("networkidle", timeout=30_000)

    log.info("Raiz: %s", root_folder_path)
    final_path, entries = navigate(context, site_url, base, root_folder_path, nav_steps)
    _navigate_browser_to(page, base, final_path)

    downloaded: list[Path] = []
    if dest_dir is not None:
        log.info("Download -> %s", dest_dir)
        downloaded = download_files(context, entries, record, dest_dir, skip_dirs=skip_dirs)

    return final_path, entries, downloaded


# ---------------------------------------------------------------------------
# Upload de arquivos para SharePoint
# ---------------------------------------------------------------------------

def _get_form_digest(context, site_url: str) -> str:
    """Obtém o X-RequestDigest necessário para operações POST no SharePoint."""
    resp = context.request.post(
        f"{site_url}/_api/contextinfo",
        headers={"Accept": "application/json;odata=verbose"},
    )
    if not resp.ok:
        raise RuntimeError(f"Erro {resp.status} ao obter FormDigest: {resp.text()[:300]}")
    return resp.json()["d"]["GetContextWebInformation"]["FormDigestValue"]


def create_file_sharing_link(
    context, site_url: str, server_relative_url: str, can_edit: bool = True,
) -> dict:
    """
    Cria um link de compartilhamento anônimo restrito a ESTE arquivo — quem
    abre não enxerga a pasta nem os arquivos de outros fornecedores. Sem
    isso, o link mandado ao fornecedor caía no atalho de pasta (`:f:`) usado
    como fallback em `commons/messaging/messenger.py`.

    `can_edit=True` porque o fornecedor precisa preencher a coluna de preço
    na planilha; `can_edit=False` gera link só de leitura.

    `linkKind` vem do enum CSOM `SharingLinkKind` (Uninitialized=0, Direct=1,
    OrganizationView=2, OrganizationEdit=3, AnonymousView=4, AnonymousEdit=5,
    Flexible=6). Sem `role` explícito — não é exigido pelo endpoint e varia
    entre versões da API; melhor não chutar. Confira o link retornado na
    primeira execução real (deve abrir só o arquivo, com "Qualquer pessoa com
    o link pode editar").
    """
    digest = _get_form_digest(context, site_url)
    api_url = (
        f"{site_url}/_api/web"
        f"/GetFileByServerRelativePath(decodedurl=@p)"
        f"/ListItemAllFields/ShareLink"
        f"?@p='{_odata_escape(server_relative_url)}'"
    )
    body = {
        "request": {
            "createLink": True,
            "settings": {
                "allowAnonymousAccess": True,
                "linkKind": 5 if can_edit else 4,
                "restrictShareMembership": False,
                "expiration": None,
            },
        },
    }
    resp = context.request.post(
        api_url,
        headers={
            "Accept": "application/json;odata=verbose",
            "Content-Type": "application/json;odata=verbose",
            "X-RequestDigest": digest,
        },
        data=json.dumps(body),
    )
    if not resp.ok:
        raise RuntimeError(
            f"Erro {resp.status} ao criar link de compartilhamento de "
            f"'{server_relative_url}': {resp.text()[:300]}"
        )

    info = resp.json().get("d", {}).get("ShareLink", {}).get("sharingLinkInfo", {})
    url = (info.get("Url") or {}).get("Value")
    if not url:
        raise RuntimeError(
            f"Resposta de ShareLink sem Url para '{server_relative_url}': "
            f"{resp.text()[:300]}"
        )
    log.info(
        "Link de compartilhamento (anonimo, %s): %s",
        'edição' if can_edit else 'leitura', url,
    )
    return {"url": url, "raw": info}


def ensure_folder_exists(context, site_url: str, folder_path: str) -> None:
    """
    Cria a hierarquia de pastas no SharePoint se não existir.
    folder_path: caminho server-relative completo (ex: '/sites/X/Shared Documents/Cotacoes/2026')
    """
    digest = _get_form_digest(context, site_url)

    site_path = urlparse(site_url).path.rstrip("/")
    parts = [p for p in folder_path.split("/") if p]
    site_parts = [p for p in site_path.split("/") if p]
    start = len(site_parts) + 1

    for i in range(start + 1, len(parts) + 1):
        partial = "/" + "/".join(parts[:i])
        check_url = (
            f"{site_url}/_api/web"
            f"/GetFolderByServerRelativePath(decodedurl=@p)"
            f"?@p='{_odata_escape(partial)}'"
        )
        resp = context.request.get(check_url, headers={"Accept": "application/json;odata=verbose"})
        if resp.ok:
            continue

        create_url = f"{site_url}/_api/web/folders"
        resp = context.request.post(
            create_url,
            headers={
                "Accept": "application/json;odata=verbose",
                "Content-Type": "application/json;odata=verbose",
                "X-RequestDigest": digest,
            },
            data=f'{{"__metadata": {{"type": "SP.Folder"}}, "ServerRelativeUrl": "{partial}"}}',
        )
        if resp.ok:
            log.info("Pasta criada: %s", partial)
        elif resp.status != 500:
            raise RuntimeError(f"Erro {resp.status} ao criar pasta '{partial}': {resp.text()[:300]}")


_LOCK_OWNER_RE = re.compile(r"bloqueado para uso compartilhado por ([^\[]+)", re.IGNORECASE)

# Tentativas de upload quando o arquivo está travado (423) por sessão do Office.
UPLOAD_LOCK_RETRIES = 4
UPLOAD_LOCK_WAIT_SECONDS = 30


def _lock_owner(body: str) -> str | None:
    """Extrai o e-mail/nome de quem está segurando o lock, se a mensagem informar."""
    match = _LOCK_OWNER_RE.search(body)
    return match.group(1).strip() if match else None


def upload_file_to_sharepoint(
    context,
    site_url: str,
    folder_path: str,
    file_path: Path,
    overwrite: bool = True,
) -> dict:
    """
    Faz upload de um arquivo para uma pasta no SharePoint via REST API.

    Se o arquivo estiver aberto no Excel (lock de coautoria), o SharePoint responde
    423 — nesse caso tenta novamente algumas vezes antes de desistir.

    Retorna dict com:
      - server_relative_url: caminho do arquivo no SharePoint
      - file_name: nome do arquivo
    """
    file_bytes = file_path.read_bytes()
    file_name = file_path.name

    ow = "true" if overwrite else "false"
    upload_url = (
        f"{site_url}/_api/web"
        f"/GetFolderByServerRelativePath(decodedurl=@p)"
        f"/Files/add(url=@f,overwrite={ow})"
        f"?@p='{_odata_escape(folder_path)}'"
        f"&@f='{urlquote(_odata_escape(file_name))}'"
    )

    resp = None
    for attempt in range(1, UPLOAD_LOCK_RETRIES + 1):
        digest = _get_form_digest(context, site_url)
        resp = context.request.post(
            upload_url,
            headers={
                "Accept": "application/json;odata=verbose",
                "X-RequestDigest": digest,
                "Content-Length": str(len(file_bytes)),
            },
            data=file_bytes,
        )
        if resp.ok or resp.status != 423:
            break

        owner = _lock_owner(resp.text()) or "outro usuário"
        if attempt < UPLOAD_LOCK_RETRIES:
            log.warning(
                "'%s' travado por %s. Nova tentativa em %ss (%s/%s).",
                file_name, owner, UPLOAD_LOCK_WAIT_SECONDS, attempt, UPLOAD_LOCK_RETRIES - 1,
            )
            time.sleep(UPLOAD_LOCK_WAIT_SECONDS)

    if not resp.ok:
        if resp.status == 423:
            owner = _lock_owner(resp.text()) or "outro usuário"
            raise RuntimeError(
                f"'{file_name}' está aberto no Excel por {owner} e o SharePoint "
                f"não libera a gravação. Feche o arquivo (Excel desktop e Excel Online) "
                f"e rode o fluxo de novo."
            )
        raise RuntimeError(
            f"Erro {resp.status} no upload de '{file_name}' para '{folder_path}': "
            f"{resp.text()[:300]}"
        )

    result_data = resp.json().get("d", {})
    server_relative_url = result_data.get("ServerRelativeUrl", f"{folder_path}/{file_name}")

    log.info("Upload: %s -> %s", file_name, server_relative_url)
    return {
        "server_relative_url": server_relative_url,
        "file_name": file_name,
    }


def get_file_sharing_url(context, site_url: str, server_relative_url: str) -> str | None:
    """
    Obtém a URL de compartilhamento (LinkingUri) de um arquivo no SharePoint.
    Retorna a URL completa ou None se não disponível.
    """
    api_url = (
        f"{site_url}/_api/web"
        f"/GetFileByServerRelativePath(decodedurl=@p)"
        f"?@p='{_odata_escape(server_relative_url)}'"
        f"&$select=LinkingUri"
    )
    try:
        resp = context.request.get(api_url, headers={"Accept": "application/json;odata=verbose"})
        if resp.ok:
            linking_uri = resp.json().get("d", {}).get("LinkingUri")
            if linking_uri:
                log.info("Link de compartilhamento: %s", linking_uri)
                return linking_uri
    except Exception as exc:
        log.error("Erro ao obter link de compartilhamento: %s", exc)
    return None


def download_single_file(context, site_url: str, server_relative_url: str, dest_path: Path) -> Path:
    """Baixa um arquivo específico do SharePoint pelo caminho server-relative."""
    download_url = (
        f"{site_url}/_api/web"
        f"/GetFileByServerRelativePath(decodedurl=@p)/$value"
        f"?@p='{_odata_escape(server_relative_url)}'"
    )
    resp = context.request.get(download_url)
    if not resp.ok:
        raise RuntimeError(
            f"Erro {resp.status} ao baixar '{server_relative_url}': {resp.text()[:300]}"
        )

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(resp.body())
    size_kb = dest_path.stat().st_size / 1024
    log.info("Download: %s (%.1f KB)", dest_path.name, size_kb)
    return dest_path


def get_file_metadata(context, site_url: str, server_relative_url: str) -> dict:
    """Retorna metadados de um arquivo no SharePoint (Name, TimeLastModified, Length, etc)."""
    api_url = (
        f"{site_url}/_api/web"
        f"/GetFileByServerRelativePath(decodedurl=@p)"
        f"?@p='{_odata_escape(server_relative_url)}'"
    )
    resp = context.request.get(api_url, headers={"Accept": "application/json;odata=verbose"})
    if not resp.ok:
        raise RuntimeError(
            f"Erro {resp.status} ao obter metadados de '{server_relative_url}': "
            f"{resp.text()[:300]}"
        )
    return resp.json().get("d", {})


def open_sharepoint_session(username: str, password: str, site_url: str, headless: bool = True):
    """
    Abre uma sessão Playwright autenticada no SharePoint.
    Retorna (playwright, browser, context, page) — o chamador deve fechar com browser.close().
    Se o login falhar, fecha o navegador e para o Playwright antes de propagar o erro:
    um `sync_playwright` que fica ativo deixa um loop asyncio na thread e a proxima
    sessao Playwright do processo falha com "Sync API inside the asyncio loop".
    """
    from commons.catapult import fechar_browser, parar_playwright
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = None
    try:
        browser = _lancar_chromium(pw, headless)
        context = _novo_contexto(browser)
        page = context.new_page()

        page.goto(site_url, wait_until="domcontentloaded", timeout=60_000)
        _handle_microsoft_login(page, username, password)

        parsed = urlparse(site_url)
        if parsed.netloc not in page.url:
            page.wait_for_url(f"**{parsed.netloc}**", timeout=60_000)
        page.wait_for_load_state("load", timeout=60_000)
    except Exception:
        fechar_browser(browser)
        parar_playwright(pw)
        raise
    log.info("[login] Sessao SharePoint aberta.")

    return pw, browser, context, page


def process_all_configs(
    records: list[dict],
    username: str,
    password: str,
    resolve_references: Callable[[dict], list[date]],
    headless: bool = True,
    keep_open: bool = False,
    download_dir: Path | None = None,
    skip_dirs: list[Path] | None = None,
    on_record_done: Callable[[dict], None] | None = None,
) -> list[dict]:
    """
    Abre o Chromium UMA VEZ, faz login e processa cada config em sequência.
    Retorna lista de dicionários com record, status, final_path e entries.

    `resolve_references(record)` devolve as semanas a navegar para o record
    (hoje: a anterior e a atual). Cada par (record, semana) gera um resultado,
    que carrega a semana em `"referencia"` para quem grava o caso.
    """
    from playwright.sync_api import sync_playwright

    credentials = (username, password)
    results = []

    with sync_playwright() as pw:
        browser = _lancar_chromium(pw, headless)
        context = _novo_contexto(browser)
        page = context.new_page()

        pares = [(record, ref) for record in records for ref in resolve_references(record)]
        for record, referencia in pares:
            nav_steps = build_nav_steps(referencia)

            log.info("[%s] %s - semana de %s (pasta %s)",
                     record['id'], record['name'], referencia.strftime('%d/%m/%Y'),
                     nome_pasta_semana(referencia))

            status = False
            final_path: str | None = None
            entries: list[dict] = []

            downloaded: list[Path] = []
            error_msg: str | None = None
            error_tipo: str | None = None
            try:
                final_path, entries, downloaded = _process_record(
                    context, page, record, nav_steps, credentials,
                    dest_dir=download_dir, skip_dirs=skip_dirs,
                )
                status = True
                log.info("Pasta final : %s", final_path)
                log.info("Itens encontrados: %s", len(entries))
                for e in entries:
                    if e["type"] == "pasta":
                        log.info("[pasta ] %s (%s itens)", e['name'], e.get('item_count', '?'))
                    else:
                        size = f"  {e['size_bytes']:,} B" if e.get("size_bytes") else ""
                        mod = f"  [{e['last_modified'][:10]}]" if e.get("last_modified") else ""
                        log.info("[arquivo] %s%s%s", e['name'], size, mod)
                if downloaded:
                    log.info("%s arquivo(s) baixado(s).", len(downloaded))
            except SharePointLoginError as exc:
                error_msg = str(exc)
                error_tipo = "login"
                log.info("ERRO DE LOGIN: %s", error_msg)
            except Exception as exc:
                error_msg = str(exc)
                error_tipo = "navegacao"
                log.info("ERRO: %s", error_msg)

            result = {
                "record": record,
                "status": status,
                "final_path": final_path,
                "error": error_msg,
                "error_tipo": error_tipo,
                "entries": entries,
                "downloaded": [str(p) for p in downloaded],
                "referencia": referencia,
            }
            results.append(result)

            # Salva no banco imediatamente, antes do browser fechar
            if on_record_done:
                on_record_done(result)

        if keep_open:
            input("\nBrowser aberto. Pressione Enter para fechar...")
        browser.close()

    return results

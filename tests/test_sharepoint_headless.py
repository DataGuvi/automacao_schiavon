"""Spec headless-sharepoint: padrao headless, viewport largo, downloads habilitados."""

import inspect

from commons import sharepoint


class _Browser:
    def __init__(self):
        self.kwargs = None

    def new_context(self, **kwargs):
        self.kwargs = kwargs
        return object()


def test_process_all_configs_e_headless_por_padrao():
    padrao = inspect.signature(sharepoint.process_all_configs).parameters["headless"].default
    assert padrao is True


def test_open_sharepoint_session_e_headless_por_padrao():
    padrao = inspect.signature(sharepoint.open_sharepoint_session).parameters["headless"].default
    assert padrao is True


def test_novo_contexto_usa_viewport_largo_e_downloads():
    browser = _Browser()
    sharepoint._novo_contexto(browser)
    assert browser.kwargs["viewport"] == {"width": 1920, "height": 945}
    assert browser.kwargs["accept_downloads"] is True


def test_login_que_falha_fecha_browser_e_para_playwright(monkeypatch):
    """Sem isso, o Playwright fica ativo e a proxima sessao do processo quebra
    com 'Sync API inside the asyncio loop'."""
    import sys
    import types

    from commons import sharepoint as sp

    eventos = []

    class Pagina:
        def goto(self, *a, **k): pass

    class Contexto:
        def new_page(self): return Pagina()

    class Browser:
        def new_context(self, **k): return Contexto()
        def close(self): eventos.append("browser.close")

    class PW:
        chromium = types.SimpleNamespace(launch=lambda **k: Browser())
        def stop(self): eventos.append("pw.stop")

    fake = types.ModuleType("playwright.sync_api")
    fake.sync_playwright = lambda: types.SimpleNamespace(start=lambda: PW())
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)

    def recusa(*a, **k):
        raise sp.SharePointLoginError("recusado")

    monkeypatch.setattr(sp, "_handle_microsoft_login", recusa)
    import types

    import pytest
    with pytest.raises(sp.SharePointLoginError):
        sp.open_sharepoint_session("u", "s", "https://x.sharepoint.com/a")
    assert eventos == ["browser.close", "pw.stop"]


def test_preencher_usuario_redigita_se_o_fill_nao_registrou():
    from commons import sharepoint as sp

    class Campo:
        def __init__(self):
            self.valor = ""
            self.digitado = None
        def fill(self, v): self.valor = "" if v == "" else ""  # fill nao "pega"
        def input_value(self): return self.valor
        def press_sequentially(self, v, delay=0): self.digitado = v; self.valor = v

    campo = Campo()
    page = type("P", (), {"locator": lambda self, sel: campo})()
    sp._preencher_usuario(page, "a@b.com")
    assert campo.digitado == "a@b.com" and campo.valor == "a@b.com"


def test_texto_erro_visivel_so_devolve_se_visivel():
    from commons import sharepoint as sp

    class El:
        def __init__(self, visivel): self.v = visivel
        def is_visible(self): return self.v
        def inner_text(self): return "senha errada"

    def pagina(el):
        return type("P", (), {"query_selector": lambda self, sel: el})()

    assert sp._texto_erro_visivel(pagina(El(True)), "#passwordError") == "senha errada"
    assert sp._texto_erro_visivel(pagina(El(False)), "#passwordError") == ""
    assert sp._texto_erro_visivel(pagina(None), "#passwordError") == ""


def test_pasta_nao_encontrada_diz_o_nome_esperado_sem_listar_as_existentes(monkeypatch):
    from datetime import date

    import pytest
    from commons import sharepoint as sp

    def pasta(nome):
        return {"name": nome, "type": "pasta", "server_relative_url": f"/r/{nome}"}

    conteudo = {
        "/r": [pasta("Invoices Fornecedores")],
        "/r/Invoices Fornecedores": [pasta("2026")],
        "/r/2026": [pasta("_Invoices para lançamento")],
        "/r/_Invoices para lançamento": [pasta("08 AGO - 2026"), pasta("09 SET - 2026")],
    }
    monkeypatch.setattr(sp, "get_folder_contents", lambda ctx, site, caminho, base: conteudo[caminho])

    with pytest.raises(RuntimeError) as exc:
        sp.navigate(None, "s", "b", "/r", sp.build_nav_steps(date(2026, 10, 5)))
    msg = str(exc.value)
    assert "10 OUT - 2026" in msg
    assert "Dispon" not in msg and "09 SET" not in msg


def test_login_com_credencial_vazia_falha_sem_abrir_o_portal():
    import types

    import pytest
    from commons import sharepoint as sp

    pagina = types.SimpleNamespace(url="https://login.microsoftonline.com/x")
    with pytest.raises(sp.SharePointLoginError, match="USER_GUVI"):
        sp._handle_microsoft_login(pagina, "", "senha")


class _PaginaTelaSenha:
    """Pagina falsa: a tela de senha fica ativa depois de `envios_ate_ativar` reenvios."""

    def __init__(self, envios_ate_ativar, erro_usuario=None, valor_usuario="a@b.com"):
        self.envios_ate_ativar = envios_ate_ativar
        self.erro_usuario = erro_usuario
        self.valor_usuario = valor_usuario
        self.chamadas = []
        self.digitado = None

    def wait_for_selector(self, sel, **k):
        self.chamadas.append(("sel", sel))

    def wait_for_load_state(self, estado, timeout):
        self.chamadas.append(("load", estado))

    def wait_for_function(self, js, timeout):
        from playwright.sync_api import TimeoutError as PwTimeout
        self.chamadas.append(("fn", js))
        if self.envios_ate_ativar > 0:
            raise PwTimeout("sem displayName")

    def wait_for_timeout(self, ms):
        self.chamadas.append(("pausa", ms))

    def query_selector(self, sel):
        if sel == "#usernameError" and self.erro_usuario:
            return type("El", (), {"is_visible": lambda s: True,
                                   "inner_text": lambda s: self.erro_usuario})()
        if sel == 'input[name="loginfmt"]':
            return self.locator(sel)
        return None

    def locator(self, sel):
        pagina = self

        class Campo:
            def fill(self, v): pass
            def is_visible(self): return True
            def input_value(self): return pagina.valor_usuario
            def press_sequentially(self, v, delay=0):
                pagina.digitado = v
                pagina.valor_usuario = v
        return Campo()

    def click(self, sel):
        self.chamadas.append(("click", sel))
        self.envios_ate_ativar -= 1


def test_aguardar_tela_senha_segue_quando_o_marcador_vem():
    from commons import sharepoint as sp

    pagina = _PaginaTelaSenha(0)
    sp._aguardar_tela_senha(pagina, "a@b.com")
    assert ("fn", sp._JS_TELA_SENHA) in pagina.chamadas
    assert ("pausa", 500) in pagina.chamadas
    assert pagina.digitado is None


def test_aguardar_tela_senha_reenvia_o_usuario_uma_vez(monkeypatch):
    from commons import sharepoint as sp

    monkeypatch.setattr(sp, "_debug_dump", lambda *a: None)
    pagina = _PaginaTelaSenha(1)
    sp._aguardar_tela_senha(pagina, "a@b.com")
    assert pagina.digitado == "a@b.com"
    assert ("click", "#idSIButton9") in pagina.chamadas


def test_aguardar_tela_senha_falha_em_vez_de_enviar_senha_na_tela_errada(monkeypatch):
    import pytest
    from commons import sharepoint as sp

    monkeypatch.setattr(sp, "_debug_dump", lambda *a: None)
    with pytest.raises(sp.SharePointLoginError, match="tela de senha"):
        sp._aguardar_tela_senha(_PaginaTelaSenha(5), "a@b.com")


def test_aguardar_tela_senha_recusa_com_erro_de_usuario(monkeypatch):
    import pytest
    from commons import sharepoint as sp

    monkeypatch.setattr(sp, "_debug_dump", lambda *a: None)
    pagina = _PaginaTelaSenha(5, erro_usuario="conta nao existe")
    with pytest.raises(sp.SharePointLoginError, match="conta nao existe"):
        sp._aguardar_tela_senha(pagina, "a@b.com")
    assert pagina.digitado is None


def test_erro_de_usuario_com_campo_vazio_nao_e_recusa_reenvia(monkeypatch):
    """Visto no servidor: a pagina apagou o e-mail e mostrou 'e-mail invalido'."""
    from commons import sharepoint as sp

    monkeypatch.setattr(sp, "_debug_dump", lambda *a: None)
    pagina = _PaginaTelaSenha(1, erro_usuario="Insira um email valido", valor_usuario="")
    sp._aguardar_tela_senha(pagina, "a@b.com")
    assert pagina.digitado == "a@b.com"


def test_enviar_usuario_redigita_se_a_pagina_limpou_o_campo():
    from commons import sharepoint as sp

    pagina = _PaginaTelaSenha(0, valor_usuario="")  # fill nao fica: pagina limpa
    sp._enviar_usuario(pagina, "a@b.com")
    assert pagina.digitado == "a@b.com"
    assert pagina.chamadas[-1] == ("click", "#idSIButton9")

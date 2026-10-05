"""Notificacao por e-mail: coletor de erros e envio ao cliente, sem SMTP real."""

from __future__ import annotations

import pytest

from commons.exception import ConfigException
from domain.config import DESTINATARIOS_CLIENTE, Config
from domain.service import notificacao_service as ns

BANCO = {"HOST": "h", "PORT": "5432", "DATABASE": "d", "USER_GUVI": "u", "PASSWORD_GUVI": "p"}


@pytest.fixture(autouse=True)
def _limpa():
    ns.limpar_erros()
    yield
    ns.limpar_erros()


@pytest.fixture
def enviados(monkeypatch):
    chamadas = []

    def falso(smtp, destino, assunto, html, txt=None, anexos=()):
        chamadas.append({"destino": destino, "assunto": assunto, "html": html,
                         "anexos": list(anexos)})
        return {"status": "sent"}

    monkeypatch.setattr(ns, "enviar_email", falso)
    return chamadas


def _cfg(**extra) -> Config:
    return Config.de_valores({**BANCO, **extra})


def test_bpo_e_dataguvi_recebem_os_relatorios():
    """BPO ligado em 2026-10-02 (commit 94a904e)."""
    assert "bpo@rokkasmarket.com" in DESTINATARIOS_CLIENTE
    assert "cauet.menezes@dataguvi.com.br" in DESTINATARIOS_CLIENTE


def test_sem_erros_nao_envia(enviados):
    assert ns.enviar_erros(_cfg()) is False
    assert enviados == []


def test_varios_erros_um_email_com_traceback(enviados):
    try:
        raise ValueError("elemento nao encontrado")
    except ValueError as exc:
        ns.registrar_erro("Catapult nota 1", exc)
    ns.registrar_erro("BD nota 2", mensagem="conexao caiu")
    assert ns.enviar_erros(_cfg()) is True
    assert len(enviados) == 1
    e = enviados[0]
    assert e["destino"] == "dataguvi@gmail.com"
    assert "2 falha(s)" in e["assunto"]
    assert "Traceback" in e["html"] and "elemento nao encontrado" in e["html"]
    assert "BD nota 2" in e["html"]
    # coletor limpo apos o envio
    assert ns.enviar_erros(_cfg()) is False


def test_destino_de_erro_vem_do_profile(enviados):
    ns.registrar_erro("x", mensagem="y")
    ns.enviar_erros(_cfg(ALERTA_EMAIL="ops@dg.com"))
    assert enviados[0]["destino"] == "ops@dg.com"


def test_enviar_erros_nunca_levanta_sem_smtp(monkeypatch):
    def sem_smtp(*a, **k):
        raise ConfigException("SMTP nao configurado")

    monkeypatch.setattr(ns, "enviar_email", sem_smtp)
    ns.registrar_erro("x", mensagem="y")
    assert ns.enviar_erros(_cfg()) is False


def _rel(tmp_path, invoice="1", divergencia=False, nome=None):
    doc = tmp_path / (nome or f"r_{invoice}.docx")
    doc.write_bytes(b"x")
    return ns.RelatorioNota(
        invoice=invoice, fornecedor="Forn SA", loja="Windermere",
        divergencia=divergencia, caminho=doc, itens=3, itens_divergentes=1 if divergencia else 0,
    )


def test_cliente_sem_relatorios_nao_envia(enviados):
    assert ns.enviar_relatorios_cliente(_cfg(), []) == 0
    assert enviados == []


def test_um_email_por_invoice_com_assunto_por_tipo(enviados, tmp_path):
    rels = [_rel(tmp_path, "10", divergencia=True), _rel(tmp_path, "11", divergencia=False)]
    assert ns.enviar_relatorios_cliente(_cfg(), rels) == 2
    assert len(enviados) == 2
    div, ok = enviados[0], enviados[1]
    assert "Divergência" in div["assunto"] and "Invoice 10" in div["assunto"]
    assert "Conciliação" in ok["assunto"] and "Invoice 11" in ok["assunto"]
    assert div["anexos"] == [rels[0].caminho] and ok["anexos"] == [rels[1].caminho]
    assert all(c["destino"] == ", ".join(DESTINATARIOS_CLIENTE) for c in enviados)


def test_varios_destinatarios_no_mesmo_email(enviados, tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "DESTINATARIOS_CLIENTE", ("a@x.com", "b@x.com"))
    assert ns.enviar_relatorios_cliente(_cfg(), [_rel(tmp_path, "1"), _rel(tmp_path, "2")]) == 2
    assert [c["destino"] for c in enviados] == ["a@x.com, b@x.com"] * 2


def test_falha_numa_nota_nao_impede_as_outras(monkeypatch, tmp_path):
    chamadas = []

    def falso(smtp, destino, assunto, html, txt=None, anexos=()):
        chamadas.append(assunto)
        return {"status": "error", "error": "fora"} if "Invoice 1 " in assunto else {"status": "sent"}

    monkeypatch.setattr(ns, "enviar_email", falso)
    rels = [_rel(tmp_path, "1"), _rel(tmp_path, "2")]
    assert ns.enviar_relatorios_cliente(_cfg(), rels) == 1
    assert len(chamadas) == 2
    assert len(ns._ERROS) == 1 and "nota 1" in ns._ERROS[0].contexto


def test_anexo_acima_do_limite_vai_sem_anexo_e_registra(enviados, tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "LIMITE_ANEXO_BYTES", 0)
    rel = _rel(tmp_path)
    assert ns.enviar_relatorios_cliente(_cfg(), [rel]) == 1
    assert enviados[0]["anexos"] == []
    assert "nao anexado" in enviados[0]["html"]
    assert "anexo grande demais" in ns._ERROS[0].contexto


def test_falha_do_email_de_erro_nao_se_registra_de_novo(monkeypatch):
    monkeypatch.setattr(
        ns, "enviar_email", lambda *a, **k: {"status": "error", "error": "fora"})
    ns.registrar_erro("x", mensagem="y")
    assert ns.enviar_erros(_cfg()) is False
    assert ns._ERROS == []


def test_email_client_anexa_arquivo_e_ignora_ausente(monkeypatch, tmp_path):
    from commons import email_client as ec

    capturado = {}

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): capturado["msg"] = msg

    monkeypatch.setattr(ec.smtplib, "SMTP", FakeSMTP)
    doc = tmp_path / "r.docx"
    doc.write_bytes(b"conteudo")
    smtp = ec.ConfigSmtp("h", "587", "u", "s", "from@x.com")
    res = ec.enviar_email(smtp, "a@b.com", "assunto", "<p>oi</p>",
                          anexos=[doc, tmp_path / "nao_existe.docx"])
    assert res["status"] == "sent"
    nomes = [p.get_filename() for p in capturado["msg"].walk() if p.get_filename()]
    assert nomes == ["r.docx"]


@pytest.mark.parametrize("porta, usa_ssl", [("465", True), ("587", False)])
def test_email_client_porta_465_usa_ssl_e_587_usa_starttls(monkeypatch, porta, usa_ssl):
    from commons import email_client as ec

    usado = []

    class Fake:
        def __init__(self, nome):
            self.nome = nome
        def __call__(self, *a, **k):
            usado.append(self.nome)
            return self
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): usado.append("starttls")
        def login(self, *a): pass
        def send_message(self, msg): pass

    monkeypatch.setattr(ec.smtplib, "SMTP_SSL", Fake("ssl"))
    monkeypatch.setattr(ec.smtplib, "SMTP", Fake("plain"))
    smtp = ec.ConfigSmtp("h", porta, "u", "s", "f@x.com")
    assert ec.enviar_email(smtp, "a@b.com", "x", "<p>y</p>")["status"] == "sent"
    assert usado == (["ssl"] if usa_ssl else ["plain", "starttls"])


def test_anexo_ilegivel_nao_levanta_e_e_listado(monkeypatch, tmp_path):
    from pathlib import Path

    from commons import email_client as ec

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): pass

    def trava(self):
        raise PermissionError("travado")

    monkeypatch.setattr(ec.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(Path, "read_bytes", trava)
    doc = tmp_path / "r.docx"
    doc.write_bytes(b"x")
    smtp = ec.ConfigSmtp("h", "587", "u", "s", "f@x.com")
    res = ec.enviar_email(smtp, "a@b.com", "x", "<p>y</p>", anexos=[doc])
    assert res["status"] == "sent" and res["anexos_ignorados"] == ["r.docx"]


def test_anexo_ausente_vai_sem_anexo_e_registra(enviados, tmp_path):
    rel = _rel(tmp_path)
    rel.caminho.unlink()
    assert ns.enviar_relatorios_cliente(_cfg(), [rel]) == 1
    assert enviados[0]["anexos"] == []
    assert "Relatorio em anexo" not in enviados[0]["html"]
    assert "anexo ausente" in ns._ERROS[0].contexto


def test_anexo_ignorado_pelo_transporte_registra_erro(monkeypatch, tmp_path):
    monkeypatch.setattr(ns, "enviar_email", lambda *a, **k: {
        "status": "sent", "anexos_ignorados": ["r.docx"]})
    assert ns.enviar_relatorios_cliente(_cfg(), [_rel(tmp_path)]) == 1
    assert "anexo ignorado" in ns._ERROS[0].contexto


def test_starttls_que_falha_fecha_o_socket(monkeypatch):
    from commons import email_client as ec

    fechado = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def starttls(self): raise OSError("handshake")
        def close(self): fechado.append(True)

    monkeypatch.setattr(ec.smtplib, "SMTP", FakeSMTP)
    smtp = ec.ConfigSmtp("h", "587", "u", "s", "f@x.com")
    res = ec.enviar_email(smtp, "a@b.com", "x", "<p>y</p>")
    assert res["status"] == "error" and fechado == [True]


def test_controller_envia_erros_mesmo_se_cair_entre_fluxos(monkeypatch):
    from crawler import controller

    enviou = []
    monkeypatch.setattr(controller, "imprimir_banner", lambda: None)
    monkeypatch.setattr(controller, "carregar_config", lambda: _cfg())
    monkeypatch.setattr(controller, "_rodar_fluxos",
                        lambda c, p: (_ for _ in ()).throw(RuntimeError("caiu")))
    monkeypatch.setattr(controller.notificacao_service, "enviar_erros",
                        lambda c: enviou.append(True))
    with pytest.raises(RuntimeError):
        controller.executar()
    assert enviou == [True]
    assert "controller" in ns._ERROS[0].contexto


def _fluxo_com(monkeypatch, tmp_path, ja_conciliada):
    """Roda `_gravar_resultado` com o banco e os geradores de .docx trocados."""
    from crawler.flow import reconcile_erp_flow as rf

    rel = tmp_path / "d.docx"
    rel.write_bytes(b"x")
    monkeypatch.setattr(rf, "ja_conciliada_erp", lambda conn, id_: ja_conciliada)
    monkeypatch.setattr(rf, "save_reconciliation_header", lambda conn, d: 1)
    monkeypatch.setattr(rf, "save_reconciliation_items", lambda *a: None)
    monkeypatch.setattr(rf, "_sincronizar_pendentes", lambda *a: None)
    monkeypatch.setattr(rf.proc, "concluir_etapa", lambda *a, **k: None)
    monkeypatch.setattr(rf, "gerar_relatorio_divergencia_erp", lambda h, r: rel)
    monkeypatch.setattr(rf, "gerar_relatorio_sucesso_erp", lambda h, r: None)
    header = {"id": 7, "id_loja": 1, "id_processo": 9, "invoice_number": "123"}
    resultado = {"issue_codes": [], "has_issue": True, "needs_review": False,
                 "items": [{"has_issue": True}], "po_orphans": []}
    totais = {"relatorios_erro": 0, "relatorios_gerados": 0, "sucessos_erro": 0,
              "sucessos_gerados": 0, "headers_total": 0, "headers_issue": 0,
              "items_total": 0, "items_issue": 0, "relatorios_cliente": []}
    rf._gravar_resultado(None, header, resultado, None, totais)
    return totais


def test_nota_ja_conciliada_nao_reenvia_email(monkeypatch, tmp_path):
    totais = _fluxo_com(monkeypatch, tmp_path, ja_conciliada=True)
    assert totais["relatorios_cliente"] == []
    assert totais["relatorios_gerados"] == 1  # .docx regerado, so nao vai no e-mail


def test_primeira_conciliacao_entra_na_fila_de_email(monkeypatch, tmp_path):
    totais = _fluxo_com(monkeypatch, tmp_path, ja_conciliada=False)
    assert len(totais["relatorios_cliente"]) == 1

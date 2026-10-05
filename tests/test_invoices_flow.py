"""Spec coleta-arquivos-soltos: so arquivo solto na pasta da semana conta."""

from crawler.flow.invoices_flow import _contar_arquivos


def test_subpastas_nao_contam_como_arquivo_encontrado():
    entries = [
        {"name": "LANCADAS", "type": "pasta"},
        {"name": "PENDENCIAS", "type": "pasta"},
    ]
    assert _contar_arquivos(entries) == 0


def test_pasta_da_semana_sem_fallback_para_a_semana_anterior():
    """A semana anterior e varrida por conta propria (R4); o fallback gravava
    os arquivos dela no caso da semana atual (R7). Em 02/10 a pasta certa e
    '28 A 04', que falta aqui: sem fallback, nada de devolver '21 A 27'."""
    from datetime import date

    from commons.sharepoint import resolve_week_folder

    pastas = [{"name": "21 A 27", "type": "pasta"}]
    assert resolve_week_folder(pastas, date(2026, 9, 25))["name"] == "21 A 27"
    assert resolve_week_folder(pastas, date(2026, 10, 2)) is None


def test_semana_que_vira_o_mes_casa_pelo_inicio():
    """Estrutura real: '28 A 04' dentro de SET (R8). Todos os dias da semana
    caem nela, inclusive os de outubro."""
    from datetime import date, timedelta

    from commons.sharepoint import resolve_week_folder

    pastas = [{"name": n, "type": "pasta"} for n in ("21 A 27", "28 A 04", "INSUMO")]
    for d in range(7):
        dia = date(2026, 9, 28) + timedelta(days=d)
        assert resolve_week_folder(pastas, dia)["name"] == "28 A 04", dia


def test_aceita_espacos_e_hifen_no_nome_da_semana():
    from datetime import date

    from commons.sharepoint import resolve_week_folder

    assert resolve_week_folder([{"name": "06  A  12", "type": "pasta"}], date(2026, 7, 8))
    assert resolve_week_folder([{"name": "06-12", "type": "pasta"}], date(2026, 7, 8))


def test_navegacao_usa_mes_e_ano_da_segunda_feira():
    from datetime import date

    from commons.sharepoint import build_nav_steps

    passos = build_nav_steps(date(2026, 10, 2))  # sexta; a semana comeca 28/09
    assert passos[1] == "2026"
    assert passos[3].esperado == "09 SET - 2026"
    assert passos[4].esperado == "28 A 04"
    virada_de_ano = build_nav_steps(date(2027, 1, 1))  # semana comeca 28/12/2026
    assert virada_de_ano[1] == "2026"
    assert virada_de_ano[3].esperado == "12 DEZ - 2026"


def test_data_de_corte_pula_a_semana_conciliada_a_mao():
    """R11: 28/09 tem PDFs ja conciliados a mao; so 05/10 em diante."""
    from datetime import date

    from crawler.flow.invoices_flow import _semanas_a_partir_do_corte

    assert _semanas_a_partir_do_corte([date(2026, 9, 28), date(2026, 10, 5)]) == [date(2026, 10, 5)]
    assert _semanas_a_partir_do_corte([date(2026, 10, 5), date(2026, 10, 12)]) == [
        date(2026, 10, 5), date(2026, 10, 12)]


def test_mes_aceita_abreviacao_em_ingles():
    """R16: '02 FEB - 2026' existe na Windermere."""
    from datetime import date

    from commons.sharepoint import resolve_month_folder

    pastas = [{"name": n, "type": "pasta"} for n in ("01 JAN - 2026", "02 FEB - 2026", "07 JUL- 2026")]
    assert resolve_month_folder(pastas, date(2026, 2, 2))["name"] == "02 FEB - 2026"
    assert resolve_month_folder(pastas, date(2026, 7, 6))["name"] == "07 JUL- 2026"
    assert resolve_month_folder(pastas, date(2026, 3, 2)) is None


def _registrar_navegacao_com(monkeypatch, status_atual, arquivos):
    from domain.service import invoice_service as svc

    chamadas = []
    monkeypatch.setattr(svc.proc, "status_atual", lambda conn, i: status_atual)
    monkeypatch.setattr(svc.proc, "falhar_etapa", lambda *a, **k: chamadas.append(("falhar", a[3])))
    monkeypatch.setattr(svc.proc, "concluir_etapa", lambda *a, **k: chamadas.append(("concluir", a[2])))
    monkeypatch.setattr(svc.proc, "registrar_contagem", lambda *a, **k: chamadas.append(("contagem", k)))
    svc.registrar_navegacao(None, 1, "/pasta", arquivos)
    return chamadas


def test_varredura_sem_arquivo_nao_rebaixa_semana_finalizada(monkeypatch):
    """R14: o cliente moveu as notas para LANCADAS; a semana segue FINALIZADA."""
    from domain.enums import StatusExecucao

    chamadas = _registrar_navegacao_com(monkeypatch, StatusExecucao.FINALIZADO, 0)
    assert not [c for c in chamadas if c[0] == "falhar"]
    assert ("contagem", {"encontrados": 0}) in chamadas


def test_varredura_sem_arquivo_em_semana_nao_finalizada_encerra_sem_arquivo(monkeypatch):
    from domain.enums import StatusExecucao

    chamadas = _registrar_navegacao_com(monkeypatch, StatusExecucao.ERRO_NAVEGACAO, 0)
    assert ("falhar", StatusExecucao.ENCERRADO_SEM_ARQUIVO) in chamadas


def test_contagem_soma_baixados_no_sql():
    """R13: o UPDATE acumula arquivos_baixados em vez de sobrescrever."""
    from domain.service import processo_service as proc

    sql = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, q, p): sql.append(q)

    class Conn:
        def cursor(self): return Cursor()
        def commit(self): pass

    proc.registrar_contagem(Conn(), 1, baixados=3)
    assert "COALESCE(arquivos_baixados, 0) + COALESCE(%s, 0)" in sql[0]


def test_semanas_da_coleta_sao_as_segundas_anterior_e_atual():
    from datetime import date

    from commons.sharepoint import semanas_da_coleta

    assert semanas_da_coleta(date(2026, 10, 5)) == [date(2026, 9, 28), date(2026, 10, 5)]
    assert semanas_da_coleta(date(2026, 10, 4)) == [date(2026, 9, 21), date(2026, 9, 28)]


def test_conta_so_os_arquivos_soltos():
    entries = [
        {"name": "LANCADAS", "type": "pasta"},
        {"name": "nota1.pdf", "type": "arquivo"},
        {"name": "nota2.pdf", "type": "arquivo"},
    ]
    assert _contar_arquivos(entries) == 2

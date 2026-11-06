"""Controller — orquestração do pipeline.

`executar()` roda os seis fluxos em sequência, sempre. Não há flags: o recorte
por fluxo e os atalhos avulsos saíram daqui; quem precisa de um passo isolado
chama a fachada do módulo. O que cada fluxo faz mora na sua fachada; aqui só a
ordem.

    FLUXO 1/6  Sincroniza o de-para de sinônimos de item (planilha -> banco)
    FLUXO 2/6  Conferência manual — lê o checkbox "Conferido" da planilha (planilha -> banco)
    FLUXO 2b/6 Coleta e leitura de invoices (SharePoint -> Vision -> banco)
    FLUXO 3/6  Cotação semanal de carnes (um passo do ciclo)
    FLUXO 4/6  Conciliação invoice x PO do Catapult (ERP) — toda nota
    FLUXO 5/6  Monitor — consolida status dos sistemas e alerta a operação
    FLUXO 6/6  Painel — DESATIVADO (painel_operacao.xlsx não é mais gerado)

Fino de propósito: monta o `Pipeline` (`crawler/pipeline.py`) e chama uma
fachada por fluxo. Sem argparse, sem recorte. As fachadas de fluxo migram para
`crawler/flow/` na Fase 3; até lá são importadas dos módulos de etapa.
"""

from __future__ import annotations

from commons.banner import imprimir_banner
from commons.db import conexao
from commons.logging_config import get_logger
# Conciliação cotação x invoice — desativada de propósito: a conferência
# correta de toda nota é contra o pedido (PO) no Catapult (FLUXO 4, abaixo),
# não contra a cotação semanal de carnes. Import e chamada ficam comentados,
# não removidos — se um dia a comparação por cotação voltar a ser necessária,
# está guardada aqui. Ver domain/categorias.py.
# from crawler.flow.conciliacao_flow import conciliacao_flow
from crawler.flow.conferencia_manual_flow import conferencia_manual_flow
from crawler.flow.cotacao_flow import cotacao_flow
from crawler.flow.invoices_flow import invoices_flow
from crawler.flow.monitor_flow import monitor_flow
# Painel (painel_operacao.xlsx) — desativado, ver FLUXO 6/6 abaixo.
# from crawler.flow.painel_flow import painel_flow
from crawler.flow.reconcile_erp_flow import reconcile_erp_flow
from crawler.flow.sinonimos_flow import sinonimos_flow
from crawler.pipeline import Pipeline
from domain.config import Config, carregar_config
from domain.service import notificacao_service, sistema_service
from domain.service.agendamento_service import registrar_heartbeat

log = get_logger(__name__)

TOTAL_FLUXOS = 4


def executar() -> None:
    imprimir_banner()
    config = carregar_config()
    pipeline = Pipeline(TOTAL_FLUXOS)
    try:
        _rodar_fluxos(config, pipeline)
    except Exception as exc:  # noqa: BLE001 — registra para o e-mail e repropaga
        notificacao_service.registrar_erro("controller: execucao interrompida", exc)
        raise
    finally:
        # E-mail unico com todas as falhas da execucao (ver notificacao_service).
        # No finally: sai mesmo se o controller cair entre fluxos. Nunca levanta.
        notificacao_service.enviar_erros(config)

    pipeline.resumo()


def _rodar_fluxos(config: Config, pipeline: Pipeline) -> None:

    # registra em dim_sistema quais sistemas têm credencial no profile.
    # O resultado real de login durante os fluxos sobrescreve isto.
    sistema_service.checar_ambiente(config)

    # ------------------------------------------------------------------
    # FLUXO 1/6 -- Sincroniza o de-para de sinônimos de item
    # Lê a aba De-Para da planilha do Google Sheets (SINONIMOS_SHEET_ID) e faz
    # upsert em dim_item_sinonimo, para a conciliação rodar com o vocabulário
    # atualizado. SINONIMOS_SHEET_ID ausente ou Sheets fora do ar -> pula e
    # retoma na próxima execução.
    # ------------------------------------------------------------------
    pipeline.rodar(1, "Sinônimos", lambda: sinonimos_flow(config))

    # ------------------------------------------------------------------
    # FLUXO 2/6 -- Conferência manual
    # Lê a coluna "Conferido" da planilha do Google Sheets
    # (CONFERENCIA_MANUAL_SHEET_ID) e grava fat_invoice.conferido_manual_em
    # para quem foi marcado — tira a invoice da tela "Conferências manuais"
    # e leva para "Invoices sem divergência" (spec-conferencia-manual-planilha).
    # CONFERENCIA_MANUAL_SHEET_ID ausente ou Sheets fora do ar -> pula e
    # retoma na próxima execução.
    # ------------------------------------------------------------------
    pipeline.rodar(2, "Conferência Manual", lambda: conferencia_manual_flow(config))

    # ------------------------------------------------------------------
    # FLUXO 2b/6 -- Coleta e leitura de invoices
    # Baixa os PDFs da semana no SharePoint, manda o Claude Vision ler e
    # grava em fat_invoice / fat_invoice_item.
    # ------------------------------------------------------------------
    pipeline.rodar(2, "Invoices", lambda: invoices_flow(config))

    # ------------------------------------------------------------------
    # FLUXO 3/6 -- Cotação semanal de carnes
    # Avança UM passo do ciclo: abre a semana, ou verifica respostas e cobra
    # quem está atrasado. Não bloqueia.
    # ------------------------------------------------------------------
    #pipeline.rodar(3, "Cotação", lambda: cotacao_flow(config))

    # ------------------------------------------------------------------
    # FLUXO 4/6 -- Conciliação invoice x PO do Catapult (ERP)
    # Busca o PO da invoice no Catapult pela Invoice Reference, raspa a grade
    # Items e compara quantidade/valor item a item (commons/catapult +
    # conciliacao/reconcile_erp.py). Vale para TODA nota, carne ou não — é a
    # única comparação que sempre existe (domain/categorias.py).
    #
    # A antiga comparação contra a cotação semanal (linha abaixo) fica
    # desativada e comentada, não removida: a conferência correta da invoice
    # é contra o PO, não contra a cotação — mas se um dia isso mudar, está
    # guardada.
    # pipeline.rodar(4, "Conciliação", lambda: conciliacao_flow(config))
    # ------------------------------------------------------------------
    pipeline.rodar(3, "Conciliação ERP", lambda: reconcile_erp_flow(config))

    # ------------------------------------------------------------------
    # FLUXO 5/6 -- Monitor
    # Consolida o status de acesso dos sistemas e abre/fecha alertas à
    # operação (e-mail à GUVI, com dedupe). Lê o que os fluxos acima
    # deixaram; não depende de nenhum ter dado certo.
    # ------------------------------------------------------------------
    pipeline.rodar(4, "Monitor", lambda: monitor_flow(config))

    # ------------------------------------------------------------------
    # FLUXO 6/6 -- Relatorio Cotacao x Invoice (DESATIVADO)
    # Gerava files/relatorios/painel_operacao.xlsx. Desligado a pedido do
    # usuario (2026-10-02): o arquivo nao deve mais ser gerado. Chamada
    # comentada, nao removida, como a cotacao - religar e so descomentar
    # (e voltar TOTAL_FLUXOS para 5). Ver spec-painel-desativado.md.
    # pipeline.rodar(5, "Painel", lambda: painel_flow(config))
    # ------------------------------------------------------------------

    # Heartbeat do agendamento: registra esta execução e projeta a próxima
    # pela cron. Antes do resumo() porque ele sai com código 1.
    _gravar_heartbeat(config, pipeline.houve_erro)


def _gravar_heartbeat(config: Config, houve_erro: bool) -> None:
    """Registra a execução em `agendamento`. Nunca levanta.

    O heartbeat é contabilidade do agendador, não resultado do pipeline: se
    ele falhar, os seis fluxos já rodaram e o RESUMO ainda tem que sair. Vive
    numa função à parte porque aqui era um `try` dentro de outro `try` —
    proibido pela governança — e o `try` de dentro só existia para o `close`,
    que agora é o `with conexao`.
    """
    try:
        with conexao(config.banco) as conn:
            registrar_heartbeat(conn, "pipeline", "erro" if houve_erro else "ok")
    except Exception:  # noqa: BLE001 — ver docstring
        log.warning("controller: heartbeat nao gravado", exc_info=True)

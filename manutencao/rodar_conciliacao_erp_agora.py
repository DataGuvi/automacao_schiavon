"""Roda so o FLUXO 4 (Conciliacao ERP) isolado, sem os outros fluxos do
`main.py`/`crawler.controller` -- para reprocessar manualmente notas presas em
LER (ou marcadas REPROCESSAR_CONCILIACAO) sem esperar o cron completo.

    python -m manutencao.rodar_conciliacao_erp_agora
"""

from __future__ import annotations

from crawler.flow.reconcile_erp_flow import reconcile_erp_flow
from domain.config import carregar_config


def main() -> None:
    config = carregar_config()
    totais = reconcile_erp_flow(config)
    print(totais)


if __name__ == "__main__":
    main()

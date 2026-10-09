"""Exploração isolada do login no Catapult — descartável.

Não faz parte do pipeline. Roda à mão pra validar `commons.catapult`
(Cloudflare Access + login ECRS) contra o ambiente real antes de mapear o
resto do Catapult (ver `TASKS.md` Fase 0 — F0.3/F0.5). Abre o browser
visível, passa pelo Cloudflare Access, faz o login do Catapult e para com o
browser aberto, pra inspecionar a tela pós-login (ainda não mapeada).

    python -m manutencao.catapult_explorar               # Windermere (padrão)
    python -m manutencao.catapult_explorar drphilips
    python -m manutencao.catapult_explorar hq

Requer no profile (`resources/config-<RPA_ENV>.env`):
    ECRS_WINDERMERE / ECRS_DRPHILIPS / ECRS_HQ   - URLs (já configuradas)
    CLOUDFLARE_ACCESS_EMAIL                      - e-mail autorizado no Access,
                                                    a mesma caixa autorizada em
                                                    'python -m manutencao.gmail_oauth_setup'
    ECRS_USER / ECRS_PASSWORD                    - login do Catapult (já configurados)
"""

from __future__ import annotations

import sys

from commons.catapult import CatapultLoginError, open_catapult_session
from domain.config import carregar_config

_URLS = {
    "windermere": "ECRS_WINDERMERE",
    "drphilips": "ECRS_DRPHILIPS",
    "hq": "ECRS_HQ",
}


def main() -> None:
    alvo = sys.argv[1] if len(sys.argv) > 1 else "windermere"
    if alvo not in _URLS:
        raise SystemExit(f"loja inválida '{alvo}'. Use: {', '.join(_URLS)}")

    config = carregar_config()
    url = getattr(config.ecrs, f"url_{alvo}")
    access_email = config.ecrs.access_email
    usuario = config.ecrs.usuario
    senha = config.ecrs.senha

    faltando = [
        nome for nome, valor in (
            (_URLS[alvo], url),
            ("CLOUDFLARE_ACCESS_EMAIL", access_email),
            ("ECRS_USER", usuario),
            ("ECRS_PASSWORD", senha),
        ) if not valor
    ]
    if faltando:
        raise SystemExit(f"faltando no profile: {', '.join(faltando)}")

    print(f"Abrindo {url} ({alvo})...")
    try:
        pw, browser, page = open_catapult_session(
            url, access_email, usuario, senha, headless=False,
        )
    except CatapultLoginError as exc:
        print(f"\nFALHOU: {exc}")
        print("Veja os logs acima")
        return

    # Fragmento (#...) não sobrevive ao redirect do Cloudflare Access — some
    # no meio do caminho porque nunca chega ao servidor. Já autenticado, sem
    # redirect de login pela frente, navegar de novo pra mesma URL preserva o
    # fragmento normalmente.
    print(f"\nURL pós-login: {page.url}")
    if "#" in url:
        print(f"Navegando de novo para preservar o fragmento: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        print(f"URL atual: {page.url}")

    input("\nBrowser aberto — inspecione a tela e pressione Enter para fechar...")
    browser.close()
    pw.stop()


if __name__ == "__main__":
    main()

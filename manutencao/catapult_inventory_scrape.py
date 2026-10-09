"""Raspa o catálogo da tela Inventory (Playwright) e grava em
`dwschiavon2.dim_item_catapult`, via `domain/service/catapult_service.py`.

Reaproveita o login já validado (`commons.catapult.open_catapult_session`), não
mexe nele. Roda visível (`headless=False`) porque os seletores da grade
Inventory foram mapeados só contra este ambiente (Fase 0, mesma disciplina de
`manutencao/catapult_explorar.py`). Salva screenshot + HTML em caso de falha,
pra ajustar seletor se algo não bater.

    python -m manutencao.catapult_inventory_scrape               # Windermere
    python -m manutencao.catapult_inventory_scrape drphilips
    python -m manutencao.catapult_inventory_scrape hq

Também grava uma cópia em `files/relatorios/catapult_inventory_scrape.xlsx`,
pra inspeção visual. Só desativa (`ativo=false`) itens sumidos do catálogo
quando as 13 páginas saem inteiras — ver `captura_completa` em `main()`.

Renomeado de `catapult_inventory_test.py`: o nome antigo terminava em
`_test.py`, o padrão de coleta padrão do pytest — inofensivo hoje (não tem
`test_*` dentro), mas arriscado se um dia morasse em `tests/`, cujo contrato
é rodar sem credencial nem efeito colateral. Este script loga no Catapult de
verdade e grava no banco.
"""

from __future__ import annotations

import sys

import openpyxl

from commons.catapult import CatapultLoginError, open_catapult_session
from commons.db import connect_db
from domain.config import carregar_config
from commons.paths import REPORTS_DIR
from domain.service.catapult_service import (
    CatapultItem, contar_catapult_items, desativar_catapult_items_sumidos,
    upsert_catapult_items,
)

_URLS = {
    "windermere": "ECRS_WINDERMERE",
    "drphilips": "ECRS_DRPHILIPS",
    "hq": "ECRS_HQ",
}

# dim_loja.id — so windermere/drphilips tem linha hoje (HQ nao e uma loja
# fisica no schema). Ver domain/service/catapult_service.py.
_ID_LOJA = {
    "windermere": 1,
    "drphilips": 2,
}

_TOTAL_PAGINAS_CATALOGO = 13  # ceil(5012 / 400), confirmado contra o ambiente real
_SAIDA = REPORTS_DIR / "catapult_inventory_scrape.xlsx"


def _abrir_inventory(page) -> None:
    print("  [inventory] Abrindo Maintenance > Inventory...")
    page.click("text=Maintenance", timeout=15_000)
    page.click("text=Inventory", timeout=15_000)
    # "text=Item ID" sozinho bate em 2 elementos (o cabecalho da grade E a
    # <option> escondida do dropdown "General"/campo de busca) — o Playwright
    # as vezes resolve pro elemento invisivel e o wait nunca satisfaz. `th` s
    # o existe no cabecalho da grade.
    page.wait_for_selector("th:has-text('Item ID')", timeout=20_000)
    page.wait_for_timeout(1500)  # grade GWT termina de popular apos o header aparecer
    print("  [inventory] Tela carregada.")


def _linhas_da_pagina(page) -> list[list[str]]:
    """Le as linhas visiveis da grade pela posicao das colunas (checkbox,
    Receipt Alias, Item ID, Name, Size, Department, Brand) — sem depender de
    classe CSS, que o GWT obfusca e pode mudar entre versoes (`NB6BGPD-k-a`,
    ...). Ancora no <th> 'Item ID', unico e estavel."""
    return page.evaluate(
        """
        () => {
            const headerCell = [...document.querySelectorAll('th')]
                .find(el => el.textContent.trim() === 'Item ID');
            if (!headerCell) return [];
            const headerRow = headerCell.closest('tr');
            const table = headerRow ? headerRow.closest('table') : null;
            if (!table) return [];
            return [...table.querySelectorAll('tr')]
                .filter(r => r !== headerRow)
                .map(r => [...r.querySelectorAll('td')].map(td => td.textContent.trim()))
                .filter(cells => cells.length >= 5);
        }
        """
    )


def _tentar_proxima_pagina(page) -> bool:
    """Clica no botao de proxima pagina da grade. Retorna False se nao achar
    ou estiver desabilitado (ultima pagina).

    O id `gwt-debug-GridNavigationButton-nextButton` aparece DUPLICADO na
    pagina (um pager pertence a um grid secundario de promocao, sempre
    desabilitado) — por isso o `:not([disabled])`, sem ele o seletor por id
    sozinho fica ambiguo pro Playwright."""
    loc = page.locator("#gwt-debug-GridNavigationButton-nextButton:not([disabled])")
    if loc.count() == 0:
        return False
    loc.first.click()
    page.wait_for_timeout(1500)
    return True


def main() -> None:
    alvo = sys.argv[1] if len(sys.argv) > 1 else "windermere"
    if alvo not in _URLS:
        raise SystemExit(f"loja invalida '{alvo}'. Use: {', '.join(_URLS)}")

    config = carregar_config()
    url = getattr(config.ecrs, f"url_{alvo}")
    access_email = config.ecrs.access_email
    usuario = config.ecrs.usuario
    senha = config.ecrs.senha

    faltando = [
        nome for nome, valor in (
            (_URLS[alvo], url), ("CLOUDFLARE_ACCESS_EMAIL", access_email),
            ("ECRS_USER", usuario), ("ECRS_PASSWORD", senha),
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
        print(f"\nFALHOU no login: {exc}")
        return

    linhas: list[list[str]] = []
    captura_completa = False
    try:
        _abrir_inventory(page)
        for i in range(1, _TOTAL_PAGINAS_CATALOGO + 1):
            pagina = _linhas_da_pagina(page)
            print(f"  [inventory] pagina {i}: {len(pagina)} linha(s) lida(s)")
            linhas.extend(pagina)
            if i < _TOTAL_PAGINAS_CATALOGO and not _tentar_proxima_pagina(page):
                print("  [inventory] nao achei botao de proxima pagina, parando aqui")
                break
        else:
            captura_completa = True
    except Exception as exc:  # noqa: BLE001 — teste, quero ver o que quebrou
        print(f"\nFALHOU lendo a grade: {exc}")
    finally:
        browser.close()
        pw.stop()

    if not linhas:
        print("\nNenhuma linha capturada.")
        return

    # td's da linha: [checkbox(0), ReceiptAlias(1), icone-camera(2, vazio),
    # ItemID(3), Name(4), Size(5), Department(6), Brand(7)] — confirmado
    # contra o HTML real da grade (indice 2 e um <td> so com icone, sem
    # texto). Pega por posicao explicita, nao por slice contiguo.
    itens = [
        CatapultItem(
            catapult_item_id=linha[3], receipt_alias=linha[1] or None,
            item_name=linha[4], size=linha[5] or None,
            department=linha[6] or None, brand=linha[7] or None,
        )
        for linha in linhas if len(linha) >= 8
    ]

    # Banco primeiro — e o que importa. Excel e so inspecao visual e nao pode
    # derrubar a gravacao (arquivo aberto no Excel trava com PermissionError).
    id_loja = _ID_LOJA.get(alvo)
    if id_loja is None:
        print(f"  [banco] '{alvo}' nao tem id_loja mapeado, pulando gravacao no banco")
    else:
        conn = connect_db(config.banco)
        try:
            n = upsert_catapult_items(conn, id_loja, itens)
            print(f"  [banco] {n} item(ns) enviado(s) para dim_item_catapult")
            if captura_completa:
                sumidos = desativar_catapult_items_sumidos(
                    conn, id_loja, [it.catapult_item_id for it in itens],
                )
                print(f"  [banco] {sumidos} item(ns) desativado(s) (sumiram do catalogo)")
            else:
                print("  [banco] captura parcial — pulando desativacao de item sumido")
            # Leitura direta do banco pos-commit — confirma o que REALMENTE
            # ficou gravado, nao so o que foi enviado no upsert.
            total = contar_catapult_items(conn, id_loja)
            print(f"  [banco] dim_item_catapult agora tem {total} linha(s) ativa(s) "
                  f"para id_loja={id_loja}")
        finally:
            conn.close()

    try:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "inventory_test"
        ws.append(["Receipt Alias", "Item ID", "Name", "Size", "Department", "Brand"])
        for it in itens:
            ws.append([it.receipt_alias, it.catapult_item_id, it.item_name, it.size,
                        it.department, it.brand])
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        wb.save(_SAIDA)
        print(f"\n{len(itens)} linha(s) salva(s) em {_SAIDA}")
    except PermissionError as exc:
        print(f"\n  [excel] pulei o Excel (arquivo provavelmente aberto): {exc}")


if __name__ == "__main__":
    main()

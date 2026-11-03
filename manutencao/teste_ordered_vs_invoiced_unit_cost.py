"""Teste ao vivo, read-only: mede se "Ordered Unit Cost" seria um substituto
melhor que "Invoiced Unit Cost" no passo 2 da cascata de custo
(`spec-qtd-mismatch-custo-unitario`).

NAO grava nada: so loga no Catapult de producao, abre a grade Items de alguns
PO ja citados nas specs (Skyharvest, Eastern, Rest Depot, Florida
Distributing, Gominas) e compara, linha a linha, `Invoiced Total Cost /
Received` contra as duas colunas de custo unitario. Nenhuma celula e editada,
nenhum Save e clicado.

A pergunta: "Invoiced Unit Cost" e, por definicao do proprio Catapult,
`Invoiced Total Cost / Received` (por isso o passo 2 de hoje fica inerte -
"Limites conhecidos" de spec-qtd-mismatch-custo-unitario). "Ordered Unit
Cost" e o custo contratado/catalogo - pode genuinamente divergir mesmo quando
o valor total bate. Este script mede, com dado real, o quao perto ou longe
"Ordered Unit Cost" fica do valor implicito faturado, pra decidir se trocar a
fonte no passo 2 (pra item comum, nao so carne) pegaria divergencia real ou
so geraria ruido.

    python -m manutencao.teste_ordered_vs_invoiced_unit_cost
    python -m manutencao.teste_ordered_vs_invoiced_unit_cost --po skyharvest
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from commons.catapult import (
    CatapultLoginError,
    fechar_browser,
    open_catapult_session,
    open_purchase_order,
    open_worksheets,
    parar_playwright,
    scrape_po_items,
    search_purchase_orders_by_invoice,
    search_purchase_orders_by_supplier,
    to_po_lines,
)
from commons.matcher import ABS_TOL, PCT_TOL, within_tolerance
from domain.config import carregar_config

# PO ja investigados ao vivo nas specs (nomes exatos do Catapult, citados em
# spec-qtd-mismatch-custo-unitario e specs relacionadas). `historico=True`
# pros que ja sao sabidamente 'Committed'.
_POS = [
    {"rotulo": "Skyharvest (snack/bebida)", "nome_po": "Skyharvest-037218-HQ-RS2",
     "fornecedor": "Skyharvest", "historico": True},
    {"rotulo": "Eastern (carne)", "nome_po": "Eastern-008922-RS2",
     "fornecedor": "Eastern", "historico": False},
    {"rotulo": "Rest Depot", "nome_po": "Rest Depot-037333-HQ-RS2",
     "fornecedor": "Rest Depot", "historico": False},
    {"rotulo": "Florida Distributing (bebida)", "nome_po": "FL-Sip Mar-037215-HQ-RS2",
     "fornecedor": "Florida Distribut", "historico": False},
    {"rotulo": "Gominas", "nome_po": "Go Minas D-036925-HQ-RS2",
     "fornecedor": "Gominas", "historico": False},
]


def _achar_href(page, alvo: dict):
    """Acha o `href` do PO pelo nome exato, tentando historico e sem, nos dois
    sentidos (R6 de spec-historico-po-catapult: sao mutuamente exclusivos)."""
    tentativas = [alvo["historico"], not alvo["historico"]]
    for mostrar_historico in tentativas:
        achados = search_purchase_orders_by_supplier(
            page, alvo["fornecedor"], mostrar_historico=mostrar_historico,
        )
        for achado in achados:
            if achado["name"] == alvo["nome_po"]:
                return achado["href"], mostrar_historico
        print(f"    (supplier={alvo['fornecedor']!r} historico={mostrar_historico}: "
              f"{len(achados)} achado(s), nenhum == {alvo['nome_po']!r} -- "
              f"{', '.join(a['name'] for a in achados) or '(vazio)'})")
    return None, None


def _fmt(v: Decimal | None) -> str:
    return "-" if v is None else f"{v:.4f}"


def _processar_po(alvo: dict, po_lines) -> list[dict]:
    linhas_medidas = []
    print(f"\n{'=' * 95}\n{alvo['rotulo']}  ({alvo['nome_po']})  -- {len(po_lines)} linha(s)\n{'=' * 95}")
    cab = (f"{'Item':<42} {'Ordered':>8} {'Received':>9} {'TotalCost':>10} "
           f"{'implicito':>10} {'InvUnit':>9} {'diffInv':>9} {'OrdUnit':>9} {'diffOrd':>9}")
    print(cab)
    print("-" * len(cab))
    for po in po_lines:
        if not po.received or po.invoiced_total_cost is None:
            continue
        implicito = po.invoiced_total_cost / po.received
        diff_inv = (implicito - po.invoiced_unit_cost) if po.invoiced_unit_cost is not None else None
        diff_ord = (implicito - po.ordered_unit_cost) if po.ordered_unit_cost is not None else None
        nome = (po.item_name or "")[:42]
        print(f"{nome:<42} {_fmt(po.ordered):>8} {_fmt(po.received):>9} "
              f"{_fmt(po.invoiced_total_cost):>10} {_fmt(implicito):>10} "
              f"{_fmt(po.invoiced_unit_cost):>9} {_fmt(diff_inv):>9} "
              f"{_fmt(po.ordered_unit_cost):>9} {_fmt(diff_ord):>9}")
        linhas_medidas.append({
            "po": alvo["rotulo"], "item": po.item_name,
            "ordered": po.ordered, "received": po.received,
            "implicito": implicito,
            "invoiced_unit_cost": po.invoiced_unit_cost, "diff_inv": diff_inv,
            "ordered_unit_cost": po.ordered_unit_cost, "diff_ord": diff_ord,
            "qty_diverge": (po.ordered != po.received)
                if (po.ordered is not None and po.received is not None) else None,
        })
    return linhas_medidas


def _resumo(todas: list[dict]) -> None:
    print(f"\n{'=' * 95}\nRESUMO\n{'=' * 95}")
    com_inv = [L for L in todas if L["invoiced_unit_cost"] is not None]
    com_ord = [L for L in todas if L["ordered_unit_cost"] is not None]
    print(f"Linhas medidas (Received>0, Invoiced Total Cost presente): {len(todas)}")
    print(f"  com Invoiced Unit Cost: {len(com_inv)}/{len(todas)}")
    print(f"  com Ordered  Unit Cost: {len(com_ord)}/{len(todas)}")

    def _dentro_tolerancia(diffs, campo):
        ok = sum(1 for L in diffs if within_tolerance(L["implicito"], L[campo], ABS_TOL, PCT_TOL))
        return ok, len(diffs)

    ok_inv, n_inv = _dentro_tolerancia(com_inv, "invoiced_unit_cost")
    ok_ord, n_ord = _dentro_tolerancia(com_ord, "ordered_unit_cost")
    print(f"\nDentro da tolerancia padrao (ABS_TOL={ABS_TOL}, PCT_TOL={PCT_TOL}) "
          f"comparado ao valor implicito (Invoiced Total Cost / Received):")
    print(f"  Invoiced Unit Cost bate em {ok_inv}/{n_inv} "
          f"({'identidade quase perfeita' if n_inv and ok_inv == n_inv else 'ha divergencia'})")
    print(f"  Ordered  Unit Cost bate em {ok_ord}/{n_ord}")

    # Das linhas com Ordered Unit Cost, quantas tem Ordered != Received (
    # qtd realmente pedida != recebida -- candidatas a divergencia REAL que o
    # passo 2 de hoje (Invoiced Unit Cost) nunca pode acusar, por ser
    # identidade).
    divergentes_qtd = [L for L in com_ord if L["qty_diverge"]]
    print(f"\nLinhas com Ordered != Received (teriam chance de ORDERED Unit Cost "
          f"discordar de verdade): {len(divergentes_qtd)}/{len(com_ord)}")
    if divergentes_qtd:
        for L in divergentes_qtd:
            bate = within_tolerance(L["implicito"], L["ordered_unit_cost"], ABS_TOL, PCT_TOL)
            print(f"    - [{L['po']}] {L['item']!r}: Ordered={L['ordered']} Received={L['received']} "
                  f"implicito={_fmt(L['implicito'])} OrderedUnitCost={_fmt(L['ordered_unit_cost'])} "
                  f"-> {'bate mesmo assim' if bate else 'DIVERGE (pegaria QTY_MISMATCH_PO novo)'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--po", help="roda so o PO cujo rotulo/fornecedor contem este texto "
                                      "(ex. 'skyharvest', 'eastern')")
    args = parser.parse_args()

    alvos = _POS
    if args.po:
        alvos = [a for a in _POS if args.po.lower() in a["rotulo"].lower()
                  or args.po.lower() in a["fornecedor"].lower()]
        if not alvos:
            raise SystemExit(f"nenhum PO conhecido bate com {args.po!r}")

    config = carregar_config()
    ecrs = config.ecrs
    url = ecrs.url_drphilips
    faltando = [
        nome for nome, valor in (
            ("ECRS_DRPHILIPS", url), ("CLOUDFLARE_ACCESS_EMAIL", ecrs.access_email),
            ("ECRS_USER", ecrs.usuario), ("ECRS_PASSWORD", ecrs.senha),
        ) if not valor
    ]
    if faltando:
        raise SystemExit(f"faltando no profile: {', '.join(faltando)}")

    print(f"Read-only: nenhuma celula sera editada, nenhum Save sera clicado.")
    print(f"POs a conferir: {', '.join(a['rotulo'] for a in alvos)}")

    pw = browser = None
    todas_as_linhas: list[dict] = []
    try:
        pw, browser, page = open_catapult_session(
            url, ecrs.access_email, ecrs.usuario, ecrs.senha, headless=False,
        )
        for alvo in alvos:
            open_worksheets(page, url)
            href, historico_usado = _achar_href(page, alvo)
            if href is None:
                print(f"  x nao achei o PO {alvo['nome_po']!r} (fornecedor "
                      f"{alvo['fornecedor']!r}) -- pulando")
                continue
            open_purchase_order(page, href)
            raw = scrape_po_items(page)
            po_lines = to_po_lines(raw)
            todas_as_linhas.extend(_processar_po(alvo, po_lines))
    except CatapultLoginError as exc:
        print(f"\nFALHOU no login: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - teste manual, quero ver o que quebrou
        print(f"\nFALHOU: {exc}")
        raise
    finally:
        fechar_browser(browser)
        parar_playwright(pw)

    if todas_as_linhas:
        _resumo(todas_as_linhas)
    else:
        print("\nNenhuma linha medida (nenhum PO achado).")


if __name__ == "__main__":
    main()

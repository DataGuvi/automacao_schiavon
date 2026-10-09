"""Teste ponta a ponta: invoice x PO do Catapult (comparacao='erp').

Login no Catapult (Dr. Phillips) -> busca PO(s) 'Ordered' do fornecedor da
invoice de teste abaixo (filtro 'Supplier', Status='Ordered', 'Show History
For' desmarcado) -> se mais de um vier, desempata pelos itens
(`conciliacao.reconcile_erp.escolher_po_por_itens`) -> abre o PO escolhido ->
preenche Invoice Number/Invoice Date na aba Receiving Information
(`fill_receiving_invoice_info`, regra nova — roda ANTES de ler
Items/conciliar, sempre, mesmo que a comparação abaixo acuse divergência) ->
le a grade Items -> compara com o motor
(`conciliacao.reconcile_erp.reconcile_items_against_po`) -> imprime o
resultado. NAO grava nada no banco (exceto o Save de Invoice Number/Date no
próprio Catapult, que é o que está sendo validado aqui) — e so pra ver o
motor + a nova regra funcionando contra dado real antes de plugar no fluxo
oficial.

Roda visivel (`headless=False`), mesma disciplina de `catapult_explorar.py` /
`catapult_inventory_scrape.py`: se algo quebrar, salva screenshot + HTML pra
ajustar seletor.

    python -m manutencao.teste_conciliacao_erp

A INVOICE DE TESTE (fixture abaixo, transcrita a mao do PDF — nao roda Vision
aqui, o motor de leitura ja e testado em outro lugar; arquivo ainda em
`unprocessed_files/`, nao passou pelo pipeline ainda):

    files/unprocessed_files/Freshpoint_1294081311__001.pdf
    FreshPoint Central FL -> Rokka's Market (Dr. Phillips).
    Invoice No. 1294081311, Order/Invoice Date 9/14/26, Customer No 10379,
    P.O. Number 19767524. 2 linhas; Sub Total e Total impressos = $16.00,
    soma das Extended Price das 2 linhas bate exato — conferido a mao antes
    de gravar aqui.

    Sem anotacao manual nesta nota — handwritten_notes=False nas 2 linhas.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from commons.catapult import (
    CatapultLoginError,
    fill_receiving_invoice_info,
    open_catapult_session,
    open_purchase_order,
    open_worksheets,
    scrape_po_items,
    search_purchase_orders_by_supplier,
    to_po_lines,
)
from domain.config import carregar_config
from commons.matcher import POLine
from conciliacao.reconcile_erp import (
    PRICE_MISMATCH_PO,
    QTY_MISMATCH_PO,
    escolher_po_por_itens,
    reconcile_items_against_po,
)

# ---------------------------------------------------------------------------
# Fixture da invoice de teste — transcrita a mao do PDF (ver docstring acima).
# Mesmo formato de `fetch_invoice_items_by_headers()`
# (domain/service/conciliacao_service.py), pra bater com o que
# `reconcile_items_against_po` espera.
# ---------------------------------------------------------------------------

ARQUIVO_INVOICE = "Freshpoint_1294081311__001.pdf"
FORNECEDOR_INVOICE = "FreshPoint Central FL"
TOTAL_INVOICE = Decimal("16.00")
INVOICE_NUMBER = "1294081311"
INVOICE_DATE = date(2026, 9, 14)

INVOICE_ITEMS = [
    {
        "id": 1, "description": "Herb Rosemary (RT)",
        "quantity": Decimal("1"), "unit_price": Decimal("6.00"),
        "total_price": Decimal("6.00"), "item_code": "450065",
        "upc": None, "handwritten_notes": False,
    },
    {
        # Fuel Delivery Charge — nao e mercadoria, nao deve casar com item de PO.
        "id": 2, "description": "Fuel Delivery Charge",
        "quantity": Decimal("1"), "unit_price": Decimal("10.00"),
        "total_price": Decimal("10.00"), "item_code": "888889",
        "upc": None, "handwritten_notes": False,
    },
]

def _achar_po(page) -> tuple[dict, list[dict]] | None:
    """Busca PO(s) 'Ordered' do fornecedor de teste. Sem nenhum, devolve
    `None`. Com um só, devolve ele direto (ainda sem raspar Items). Com mais
    de um, abre e raspa Items de CADA candidato e desempata pelos itens da
    invoice de teste (`escolher_po_por_itens`) — devolve o escolhido já com
    `raw_rows` raspado, pra `main()` não precisar raspar de novo.

    `search_purchase_orders_by_supplier` levanta `RuntimeError` quando a
    busca parece nao ter sido aplicada de verdade (grade voltou pra listagem
    padrao)."""
    print(f"  [worksheets] buscando Supplier contains '{FORNECEDOR_INVOICE}', "
          "Status=Ordered...")
    achados = search_purchase_orders_by_supplier(page, FORNECEDOR_INVOICE)
    if not achados:
        print("    nada encontrado")
        return None

    print(f"    {len(achados)} PO(s) 'Ordered' encontrado(s):")
    for po in achados:
        print(f"      - {po['name']}")

    if len(achados) == 1:
        return achados[0], None

    print("    mais de um PO — raspando cada um pra desempatar pelos itens...")
    candidatos_raw: list[list[dict]] = []
    candidatos_po_lines: list[list[POLine]] = []
    for achado in achados:
        open_purchase_order(page, achado["href"])
        raw = scrape_po_items(page)
        candidatos_raw.append(raw)
        candidatos_po_lines.append(to_po_lines(raw))

    indice = escolher_po_por_itens(INVOICE_ITEMS, candidatos_po_lines)
    if indice is None:
        print("    ⚠ nenhum candidato casou com os itens da invoice de teste — "
              "usando o primeiro mesmo assim (só pra debug).")
        indice = 0
    print(f"    escolhido: {achados[indice]['name']}")
    return achados[indice], candidatos_raw[indice]


def main() -> None:
    config = carregar_config()
    url = config.ecrs.url_drphilips
    access_email = config.ecrs.access_email
    usuario = config.ecrs.usuario
    senha = config.ecrs.senha

    faltando = [
        nome for nome, valor in (
            ("ECRS_DRPHILIPS", url), ("CLOUDFLARE_ACCESS_EMAIL", access_email),
            ("ECRS_USER", usuario), ("ECRS_PASSWORD", senha),
        ) if not valor
    ]
    if faltando:
        raise SystemExit(f"faltando no profile: {', '.join(faltando)}")

    print(f"{'='*70}")
    print(f"Invoice de teste : {ARQUIVO_INVOICE}")
    print(f"Fornecedor       : {FORNECEDOR_INVOICE}")
    print(f"Total            : ${TOTAL_INVOICE}")
    print(f"Itens            : {len(INVOICE_ITEMS)}")
    for item in INVOICE_ITEMS:
        print(f"  - {item['description']}  upc={item['upc']}  "
              f"qtd={item['quantity']}  total=${item['total_price']}")
    print(f"{'='*70}\n")

    print(f"Abrindo {url} (Dr. Phillips)...")
    try:
        pw, browser, page = open_catapult_session(
            url, access_email, usuario, senha, headless=False,
        )
    except CatapultLoginError as exc:
        print(f"\nFALHOU no login: {exc}")
        return

    try:
        open_worksheets(page, url)
        achado = _achar_po(page)
        if achado is None:
            print(f"\nNenhum PO 'Ordered' encontrado pro fornecedor {FORNECEDOR_INVOICE!r}.")
            print(f"Ajuste FORNECEDOR_INVOICE em {__file__} e rode de novo.")
            return
        po_encontrado, raw_rows_ja_raspado = achado

        print(f"\n  [po] abrindo {po_encontrado['name']}...")
        open_purchase_order(page, po_encontrado["href"])

        print(f"  [po] preenchendo Receiving Information "
              f"(Invoice Number={INVOICE_NUMBER!r}, Invoice Date={INVOICE_DATE.isoformat()})...")
        fill_receiving_invoice_info(page, INVOICE_NUMBER, INVOICE_DATE)
        print("  [po] Receiving Information salvo (ou PO ja Committed - ver aviso acima).")

        raw_rows = raw_rows_ja_raspado if raw_rows_ja_raspado is not None else scrape_po_items(page)
        print(f"  [po] {len(raw_rows)} item(ns) lido(s) da grade Items:")
        for row in raw_rows:
            print(f"      - {row.get('Item Name')}  "
                  f"(receipt_alias={row.get('Receipt Alias')!r}, "
                  f"scancode={row.get('Scancode')}, "
                  f"supplier_unit_id={row.get('Supplier Unit ID')})  "
                  f"ordered={row.get('Ordered')}  received={row.get('Received')}  "
                  f"invoiced_total={row.get('Invoiced Total Cost')}")

        po_lines = to_po_lines(raw_rows)

    except Exception as exc:  # noqa: BLE001 — teste, quero ver o que quebrou
        print(f"\nFALHOU raspando o PO: {exc}")
        return
    finally:
        browser.close()
        pw.stop()

    print(f"\n{'='*70}")
    print("RESULTADO DA CONCILIACAO (comparacao='erp')")
    print(f"{'='*70}")

    po_by_key = {po.key: po for po in po_lines}
    resultado = reconcile_items_against_po(INVOICE_ITEMS, po_lines)

    # Quantas linhas da invoice casaram com cada item do PO, e a soma do
    # preco delas — pra avisar quando a comparacao de preco/qtd e AGREGADA
    # (soma do grupo), nao so o valor desta linha isolada.
    linhas_por_po_key: dict = {}
    soma_invoice_por_po_key: dict = {}
    for linha in resultado["items"]:
        chave = linha.get("id_po_item")
        if chave is None:
            continue
        linhas_por_po_key[chave] = linhas_por_po_key.get(chave, 0) + 1
        soma_invoice_por_po_key[chave] = (
            soma_invoice_por_po_key.get(chave, Decimal("0"))
            + (linha.get("price_invoice") or Decimal("0"))
        )

    todos_casaram = True
    todos_precos_ok = True
    todos_qtd_ok = True
    algum_ordered_zero_conhecido = False

    for linha in resultado["items"]:
        print(f"\n  {linha['description_invoice']}")

        if linha["match_level"] == "unmatched":
            todos_casaram = False
            print("      ✗ SEM PAR NO PO — nenhum item do PO bateu por "
                  "codigo nem por nome (ver item_name/receipt_alias no PO)")
            continue

        print(f"      par no PO : {linha['item_name_po']}  "
              f"(match_level={linha['match_level']})")

        preco_ok = PRICE_MISMATCH_PO not in linha["issue_codes"]
        qtd_ok = QTY_MISMATCH_PO not in linha["issue_codes"]
        todos_precos_ok = todos_precos_ok and preco_ok
        todos_qtd_ok = todos_qtd_ok and qtd_ok

        chave = linha["id_po_item"]
        agrupado = linhas_por_po_key.get(chave, 1) > 1
        rotulo_preco = "preco confere" if preco_ok else "PRECO DIVERGE"
        marca_preco = "✓" if preco_ok else "✗"
        if agrupado:
            print(f"      {marca_preco} {rotulo_preco:<14} — esta linha=${linha['price_invoice']}  "
                  f"soma do grupo=${soma_invoice_por_po_key[chave]}  "
                  f"PO=${linha['price_other']}  dif=${linha['price_diff']}")
            print(f"        (*) {linhas_por_po_key[chave]} linha(s) da invoice casaram "
                  f"com este MESMO item do PO — 'dif' compara a SOMA do grupo com o "
                  f"PO, nao esta linha sozinha")
        else:
            print(f"      {marca_preco} {rotulo_preco:<14} — invoice=${linha['price_invoice']}  "
                  f"PO=${linha['price_other']}  dif=${linha['price_diff']}")

        po = po_by_key.get(linha["id_po_item"])
        if qtd_ok:
            print("      ✓ quantidade confere")
        elif po is not None and po.ordered == 0:
            algum_ordered_zero_conhecido = True
            print("      ✗ QUANTIDADE DIVERGE  (CONHECIDO: este fornecedor nao "
                  "usa PO previo — Ordered sempre 0 — pendente de validacao "
                  "com o cliente, nao e erro de lancamento)")
        else:
            print("      ✗ QUANTIDADE DIVERGE")

    print(f"\n{'-'*70}")
    print("RESUMO")
    print(f"{'-'*70}")
    print(f"  Todo item achou par no PO   : {'sim' if todos_casaram else 'NAO'}")
    print(f"  Preco bate em todo item     : {'sim' if todos_precos_ok else 'NAO'}")
    if todos_qtd_ok:
        print("  Quantidade bate em todo item: sim")
    elif algum_ordered_zero_conhecido:
        print("  Quantidade bate em todo item: NAO — mas so pelo caso conhecido "
              "do Ordered=0 (ver aviso acima), nao por erro de lancamento")
    else:
        print("  Quantidade bate em todo item: NAO")

    if resultado["po_orphans"]:
        nomes_orfaos = [po_by_key[k].item_name for k in resultado["po_orphans"]
                        if k in po_by_key]
        print(f"\n  {len(resultado['po_orphans'])} item(ns) do PO NAO reclamado(s) "
              f"por nenhuma linha desta invoice (recebido/pedido mas nao "
              f"faturado NESTA nota — pode ser de outra invoice do mesmo PO):")
        for nome in nomes_orfaos:
            print(f"      - {nome}")

    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()

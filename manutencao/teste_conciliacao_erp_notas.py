"""Teste ponta a ponta do FLUXO 4 com invoices reais de `files/unprocessed_files/`.

Exercita o caminho NOVO de `crawler/flow/reconcile_erp_flow.py::_buscar_po`:
busca PO 'Ordered' por `Invoice Reference` (Equals) e, se nao achar PO que
case pelos itens, cai na busca por nome (`Supplier`, com fuzzy). Depois
compara com o motor (`reconcile_items_against_po`) e imprime o resultado.

Simula por padrao: NAO escreve Invoice Number/Date no Catapult nem grava no
banco. So `--aplicar` deixa `fill_receiving_invoice_info` rodar de verdade
(escreve no Catapult de producao).

    python -m manutencao.teste_conciliacao_erp_notas --nota leblon
    python -m manutencao.teste_conciliacao_erp_notas --nota blackbull
    python -m manutencao.teste_conciliacao_erp_notas --nota gominas
    python -m manutencao.teste_conciliacao_erp_notas --nota fabifine --aplicar

Notas disponiveis (fixtures abaixo, transcritas a mao dos PDFs; nao roda
Vision). Ambas sao da loja Dr. Phillips (7720 Turkey Lake Rd). Datas no PDF
sao mes/dia/ano (spec-data-invoice-mdy).

  fabifine  fabifine_535__10082026.pdf - Fabi Fine Candies, INV-535,
            08/10/2026, 1 linha (100 x 0.80 = 80.00), total $80.00.
  blackbull Blackbull_56617__10072026.pdf - Black Bull / Saab Foods, invoice
            565617, 07/10/2026 (mes/dia/ano), 3 linhas de carne (234.97 +
            1180.61 + 451.11 = 1866.69), total $1,866.69 (bate), 10 caixas.
            Parte do nome da linha 405 esta coberta por tinta; categoria
            'carne' (compara CAIXA x Ordered, valor valida o peso).
  gominas   gominas_5114__10022026.pdf - Gominas Distribution, invoice 5114,
            02/10/2026 (mes/dia/ano), 2 linhas (24.90 + 34.00 = 58.90 de
            sub total; desconto $5.89 -> total $53.01), pack "12x80g" na
            descricao. Tem carimbo 'INSUMO' na pagina: no robo a nota e
            pulada (NAO_COMPARADO) e nem chega ao Catapult.
  leblon    Leblon_90024400__10052026.pdf - Leblon Foods, invoice 90024400,
            05/10/2026, 3 linhas (53.90 + 65.70 + 28.20 = 147.80), total
            $147.80 (batem). No PDF ha vistos, carimbo 'LANCADO', assinatura
            e um rabisco manuscrito ('Bip ok'); nenhum e anotacao de insumo,
            entao handwritten_notes=False. Caso do fuzzy: o nome lido
            ('Leblon Foods') difere do cadastrado no Catapult ('Leblon').

Cada nota tem o campo `historico`: True marca 'Show History' na busca (acha PO
ja Committed do ano da tela; o filtro Status=Ordered nao se aplica, o dropdown
fica desabilitado). Leblon roda com historico marcado; fabifine, como o robo.

Ajuste `numero` da nota se a Invoice Reference estiver gravada de outro jeito
no Catapult (o `Equals` exige o texto exato).
"""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal

from commons import catapult
from commons.catapult import (
    CatapultLoginError,
    fechar_browser,
    open_catapult_session,
    parar_playwright,
)
from crawler.flow import reconcile_erp_flow as flow
from conciliacao.reconcile_erp import (
    tem_anotacao_insumo,
    PRICE_MISMATCH_PO,
    QTY_MISMATCH_PO,
    reconcile_items_against_po,
)
from domain.config import carregar_config

def _linha(id_, descricao, qtd, preco, total, codigo=None, upc=None, cases=None,
           codigo_manual=None):
    return {
        "id": id_, "description": descricao, "quantity": Decimal(qtd),
        "unit_price": Decimal(preco), "total_price": Decimal(total),
        "item_code": codigo, "upc": upc, "handwritten_notes": False,
        "cases": None if cases is None else Decimal(cases),
        "handwritten_code": codigo_manual,
    }


NOTAS = {
    "fabifine": {
        "arquivo": "fabifine_535__10082026.pdf",
        "fornecedor": "Fabi Fine Candies",
        "numero": "INV-535",
        "data": date(2026, 10, 8),
        "total": Decimal("80.00"),
        "historico": False,
        "itens": [
            _linha(1, "Balas baianas caramelizadas", "100", "0.80", "80.00"),
        ],
    },
    "blackbull": {
        "arquivo": "Blackbull_56617__10072026.pdf",
        "fornecedor": "Black Bull - Saab Foods",
        "numero": "565617",
        "data": date(2026, 10, 7),
        "total": Decimal("1866.69"),
        "historico": False,
        "categoria": "carne",
        "itens": [
            # Carne de peso variavel: quantity = peso em lb (peso x preco = total),
            # cases = caixas, entre parenteses na coluna Quantity. O codigo
            # manuscrito ao lado da linha (40116, 40162) e o do catalogo do Rokka's.
            _linha(1, "HEEL MEAT DLX - MACESA", "50.10", "4.69", "234.97",
                   codigo="000401", cases="1"),
            # Parte do nome coberta por tinta no PDF; so o trecho legivel abaixo.
            _linha(2, "DLX - COLETA DE QUADRIL - MACESA", "275.20", "4.29", "1180.61",
                   codigo="000405", cases="5", codigo_manual="40116"),
            _linha(3, "FLAT IRON STEAK - COXAO DURO - QUADRADA OU PULPA NEGRA - MACESA",
                   "80.70", "5.59", "451.11", codigo="000184", cases="4",
                   codigo_manual="40162"),
        ],
    },
    "gominas": {
        "arquivo": "gominas_5114__10022026.pdf",
        "fornecedor": "Gominas Distribution",
        "numero": "5114",
        "data": date(2026, 10, 2),
        # Sub Total impresso ($58.90) = soma das linhas; o desconto de $5.89
        # (Total $53.01) e da nota, nao de linha.
        "total": Decimal("58.90"),
        "historico": False,
        # Carimbo impresso na pagina (nao preso a linha): a Vision o devolve em
        # `general_handwritten_notes`. Com 'insumo', o robo NAO leva a nota ao Catapult.
        "anotacao_geral": "INSUMO - Adriana Maisa Pereira 10/07/2026 11:10",
        "itens": [
            # Pack "12 x 80g" na descricao: quantity = qtd impressa x 12, cases = qtd impressa.
            _linha(1, "Fini G Amora 12 x 80g", "12", "24.90", "24.90", codigo="2314", cases="1"),
            _linha(2, "Fini G Ovo Frito 12x80g", "24", "17.00", "34.00", codigo="2914", cases="2"),
        ],
    },
    "leblon": {
        "arquivo": "Leblon_90024400__10052026.pdf",
        "fornecedor": "Leblon Foods",
        "numero": "90024400",
        "data": date(2026, 10, 5),
        "total": Decimal("147.80"),
        "historico": True,
        "itens": [
            # Leitura da Vision com o pack da coluna "Pack Size" (12/12 oz, 6/2LB,
            # 6/12 oz) aplicado: quantity = qtd impressa (1) x N; a PO do
            # Catapult (Single Unit) conta 12 / 6 / 6 unidades.
            _linha(1, "MIMOSO MINAS FRESCAL CHEESE 12oz", "12", "53.90", "53.90",
                   codigo="1", upc="819753000522", cases="1"),
            _linha(2, "DA ROCA QUEIJO COALHO 2LBS", "6", "65.70", "65.70",
                   codigo="381", upc="819753008252", cases="1"),
            _linha(3, "DA ROCA QUEIJO COALHO 12OZ", "6", "28.20", "28.20",
                   codigo="415", upc="819753008191", cases="1"),
        ],
    },
}


def _marcar_show_history() -> None:
    """Troca `_preparar_filtros_po` por uma versao com 'Show History' MARCADO.

    O robo sempre desmarca o historico (so quer POs nao Committed). Aqui
    marca, para achar PO ja Committed (do ano selecionado na tela). Com o
    historico marcado o dropdown de Status fica desabilitado (confirmado no
    Catapult real), entao o filtro Status=Ordered nao e aplicado.
    """
    def _preparar_com_historico(page, timeout):
        page.wait_for_selector(catapult._SEL_WORKSHEET_CATEGORY, timeout=timeout)
        page.select_option(catapult._SEL_WORKSHEET_CATEGORY, label="Purchase Order")
        page.wait_for_timeout(400)
        if not page.locator(catapult._SEL_SHOW_HISTORY_CHECKBOX).is_checked():
            page.locator(catapult._SEL_SHOW_HISTORY_LABEL).click(timeout=timeout)
            page.wait_for_timeout(800)
        print("  [filtros] Show History MARCADO (Status=Ordered nao aplicado)")
    catapult._preparar_filtros_po = _preparar_com_historico


def _listar_linhas_da_po() -> None:
    """Imprime as linhas cruas de cada PO raspada (Size, Unit, Ordered, Received...)."""
    raspar = flow.scrape_po_items

    def _com_print(page, *a, **k):
        linhas = raspar(page, *a, **k)
        print(f"  [po] {len(linhas)} item(ns) na grade Items:")
        for r in linhas:
            print(f"      - {r.get('Item Name')} | size={r.get('Size')} | unit={r.get('Unit')} | "
                  f"ordered={r.get('Ordered')} | received={r.get('Received')} | "
                  f"total={r.get('Invoiced Total Cost')}")
        return linhas

    flow.scrape_po_items = _com_print


def _simular_gravacao_no_catapult() -> None:
    """Troca a escrita em Receiving Information por um print (modo padrao)."""
    def _so_imprime(page, numero, data):
        print(f"  [simulacao] NAO gravou Receiving Information "
              f"(Invoice Number={numero!r}, Invoice Date={data}). Use --aplicar.")
    flow.fill_receiving_invoice_info = _so_imprime


def _observar_buscas(nota: dict) -> list[str]:
    """Registra qual busca rodou, na ordem, sem mudar o comportamento."""
    ordem: list[str] = []
    por_invoice = flow.search_purchase_orders_by_invoice
    por_nome = flow.search_purchase_orders_by_supplier

    def _invoice(*a, **k):
        achados = por_invoice(*a, **k)
        ordem.append("invoice")
        print(f"  [busca] Invoice Reference Equals {nota['numero']!r}: {len(achados)} PO(s)")
        return achados

    def _nome(*a, **k):
        achados = por_nome(*a, **k)
        ordem.append("nome")
        print(f"  [busca] fallback por nome (Supplier): {len(achados)} PO(s)")
        for po in achados:
            print(f"      - {po['name']}")
        return achados

    flow.search_purchase_orders_by_invoice = _invoice
    flow.search_purchase_orders_by_supplier = _nome
    return ordem


def _imprimir_resultado(nota: dict, po_lines) -> None:
    resultado = reconcile_items_against_po(
        nota["itens"], po_lines, categoria_fornecedor=nota.get("categoria"),
    )
    print(f"\n{'=' * 70}\nRESULTADO DA CONCILIACAO (comparacao='erp')\n{'=' * 70}")
    for linha in resultado["items"]:
        print(f"\n  {linha['description_invoice']}")
        if linha["match_level"] == "unmatched":
            print("      x SEM PAR NO PO (nem por codigo nem por nome)")
            continue
        preco_ok = PRICE_MISMATCH_PO not in linha["issue_codes"]
        qtd_ok = QTY_MISMATCH_PO not in linha["issue_codes"]
        print(f"      par no PO : {linha['item_name_po']} (match_level={linha['match_level']})")
        print(f"      {'ok' if preco_ok else 'x '} preco      invoice=${linha['price_invoice']}  "
              f"PO=${linha['price_other']}  dif=${linha['price_diff']}")
        print(f"      {'ok' if qtd_ok else 'x '} quantidade")
    print(f"\n  has_issue={resultado['has_issue']}  needs_review={resultado['needs_review']}")
    print(f"{'=' * 70}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nota", required=True, choices=sorted(NOTAS),
                        help="invoice de teste (fixture em NOTAS)")
    parser.add_argument("--aplicar", action="store_true",
                        help="grava Invoice Number/Date no Catapult (producao)")
    args = parser.parse_args()

    nota = NOTAS[args.nota]
    header = {
        "id": 0, "invoice_number": nota["numero"], "invoice_date": nota["data"],
        "supplier_name": nota["fornecedor"],
    }
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

    print(f"{'=' * 70}")
    print(f"Invoice de teste : {nota['arquivo']}")
    print(f"Fornecedor       : {nota['fornecedor']}")
    print(f"Invoice / data   : {nota['numero']} / {nota['data'].isoformat()}")
    print(f"Total            : ${nota['total']}   Itens: {len(nota['itens'])}")
    print(f"Show History     : {'marcado' if nota['historico'] else 'desmarcado (padrao do robo)'}")
    print(f"Modo             : {'APLICAR (grava no Catapult)' if args.aplicar else 'simulacao'}")
    print(f"{'=' * 70}\n")

    if tem_anotacao_insumo(nota["itens"], nota.get("anotacao_geral")):
        print("  AVISO: nota com anotacao de INSUMO. No robo ela e pulada (NAO_COMPARADO) e nao "
              "vai ao Catapult; este teste ignora a regra e busca a PO mesmo assim.\n")

    if not args.aplicar:
        _simular_gravacao_no_catapult()
    if nota["historico"]:
        _marcar_show_history()
    _listar_linhas_da_po()
    ordem = _observar_buscas(nota)

    pw = browser = None
    try:
        pw, browser, page = open_catapult_session(
            url, ecrs.access_email, ecrs.usuario, ecrs.senha, headless=False,
        )
        po_lines = flow._buscar_po(page, url, header, nota["itens"], {}, lambda nome: None)
    except CatapultLoginError as exc:
        print(f"\nFALHOU no login: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 — teste manual, quero ver o que quebrou
        print(f"\nFALHOU na busca/raspagem do PO: {exc}")
        return
    finally:
        fechar_browser(browser)
        parar_playwright(pw)

    print(f"\n  buscas executadas: {' -> '.join(ordem) or '(nenhuma)'}")
    if po_lines is None:
        print("  Resultado: nenhuma PO 'Ordered' encontrada. No robo isso poe a nota em "
              "PO_NAO_ENCONTRADA (13) e repete a consulta na proxima execucao "
              "(ate 3 vezes).")
        return
    if not po_lines:
        print("  Resultado: achou PO(s), mas nenhuma casou pelos itens (ou ha candidatos "
              "demais). No robo vira 'sem PO' direto, sem retentativa.")
        return
    _imprimir_resultado(nota, po_lines)


if __name__ == "__main__":
    main()

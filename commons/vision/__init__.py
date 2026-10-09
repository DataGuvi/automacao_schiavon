"""Leitura de invoices (PDF e imagens) via Claude Vision API."""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Literal, NamedTuple

import anthropic

from .schema import InvoiceData, InvoiceItem
from commons.logging_config import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Prompt de extração
# ---------------------------------------------------------------------------

_PROMPT = """\
You are an expert at reading and extracting structured data from vendor invoices,
including digital PDFs and scanned paper documents.

Extract ALL information visible in this invoice and return ONLY a valid JSON object
(no markdown, no explanation — raw JSON only).

Pay special attention to:

1. HANDWRITTEN ANNOTATIONS — three kinds, captured in DIFFERENT fields:
   - A short handwritten NUMBER written on or right below the line (e.g.
     "40102"), on its own, that looks like an item code — not a price, not a
     quantity correction. Some suppliers' invoices carry a hand-written code
     that is actually the BUYER's own catalog code for that item, different
     from whatever code the invoice prints. Capture this in `handwritten_code`
     (digits only). This is a real, confirmed pattern for some suppliers —
     don't skip it as noise.
   - Handwriting clearly next to/below ONE specific line — quantity/price
     corrections, return notes, approval stamps, checkmarks with initials.
     Capture as free text in `handwritten_notes` of that item.
   - Handwriting NOT tied to any specific line — written in a margin, in the
     blank space below the item table, or anywhere else on the page as a
     note about the invoice as a whole (e.g. a word or short phrase noting
     what the order is for). Capture the exact text, verbatim, in the
     invoice-level `general_handwritten_notes` field — don't drop it just
     because it has no single item to attach to, and don't paraphrase it
     into `reading_notes` instead.

2. DATES — ISO 8601 format: YYYY-MM-DD in `invoice_date` / `due_date`.
   These are US invoices: a printed date like "10/05/26" or "10/05/2026" is
   MONTH/DAY/YEAR (October 5, 2026), not day/month/year. Also copy each date
   EXACTLY as printed, character by character, into `invoice_date_raw` /
   `due_date_raw` (e.g. "10/05/26") — don't reformat it there.

3. AMOUNTS — separate subtotal, tax, and total.

4. LINE ITEMS — every row, in order.

5. ITEM CODE / UPC — some invoices have a separate column for the supplier's own
   SKU (headers like "Item", "Product", "Product or service", "SKU") — that
   is `item_code`, NOT the same as `description`. Some also show a UPC/EAN barcode,
   either in its own column ("UPC", "UPC Item") or printed next to/below the
   description — that is `upc` (digits only). When the ONLY code on the line is a
   UPC/barcode (no separate supplier SKU), put it in `upc` and leave `item_code`
   null — don't copy the same value into both. Leave both null when the invoice
   has neither.
   Don't confuse a leading "#" column with `item_code` — "#" is just the printed
   row/line number (1, 2, 3, ...) and belongs in `item_order`, never in
   `item_code`. When a line has BOTH a "#" column and a separate "SKU" (or
   "Item"/"Product") column, `item_code` always comes from the SKU/Item/Product
   column, not from "#". Example: a row "# 1  SKU 00152  QUALY MARGARINA..." →
   `item_order`=1, `item_code`="00152".

6. READING CONFIDENCE — integer 0-100:
   90-100: all clear | 70-89: minor issues | 50-69: significant issues | 0-49: major problems

7. CASES vs WEIGHT — meat/protein invoices often print a case-count column
   (labeled "CASES", "ORDER QTY", "SHIP QTY", "QTY", "CARTONS", ...) separate
   from a weight column (labeled "WEIGHT", "EXT WEIGHT", "LBS", "N.W", ...),
   with the unit price applied per pound (variable-weight/catch-weight items — pack/size
   often shows "AVG", or the case size is itself a weight like "65# CS").
   When BOTH a case-count and a weight column exist on the same line AND the
   unit price is applied per unit of weight (weight x unit_price = total_price):
   put the WEIGHT in `quantity` (so `quantity x unit_price = total_price` still
   holds) and put the case count in `cases`. If instead the unit price is per
   case/each (printed qty x unit_price = total_price), the weight column is
   informational only: never put it in `quantity`. When the invoice has only ONE
   quantity column (no separate weight, e.g. dry goods sold by the case),
   leave `cases` null — `quantity` already IS the case count — UNLESS rule 8
   below applies.

8. QUANTITY x PACK-SIZE MULTIPLIER FROM THE DESCRIPTION — many invoices sell
   by the case/pack but print the pack size right inside the description
   itself, right before or attached to a weight/volume unit, as
   "<N>X<size> <UNIT>" or "<N>X<size><UNIT>" (UNIT being a weight or volume
   unit — GR, G, KG, ML, LB, OZ, ... — e.g. "12X500 GR", "16x500 GR",
   "6X7.5 ML", "20X500G" with no space, "90X30g", "10X1 KG") — N is how many
   individual units are packed into what the Qty column counts, not a
   separate quantity. Apply this whenever the description carries this
   "<N>X<size>" pattern: the real quantity is Qty(printed) x N — regardless
   of what the unit/UN column says, or even if it's blank. That column is
   unreliable and often empty; don't gate this rule on it. Put the product
   Qty(printed) x N in `quantity`, and put the ORIGINAL printed Qty in
   `cases`. Leave `unit_price` and `total_price` exactly as printed — do NOT
   recompute `total_price` from the new `quantity` here (unlike rule 7):
   `total_price` must stay the real, literal amount the invoice charges for
   that line, because whether the multiplied `quantity` or the original
   `cases` is the one that should match the purchase order is decided later,
   downstream, against data this reading has no access to (see note below).
   Example: description "QUALY MARGARINATRAD. C/SAL 12x500 GR" with a
   printed Qty of 3 → N=12 → `quantity`=36, `cases`=3 (`total_price`
   untouched, whatever was printed for those 3 printed units).
   Example: description "LASANHA DONA BENTA 20X500G" with a printed Qty of
   1 → N=20 → `quantity`=20, `cases`=1 (`total_price` untouched).
   Example: description "Requeijao Cremoso Copo TRADICIONAL Tirolez
   12x200g" with a printed Qty of 7 → N=12 → `quantity`=84, `cases`=7.
   The pack size can also be printed in its OWN column ("Pack Size", "Pack",
   "Size") with a slash instead of an "x", as "<N>/<size> <UNIT>" — e.g.
   "12/12 oz", "6/2LB", "6/12 oz". Same rule: N is the first number, the
   real quantity is Qty(printed) x N, `cases` is the printed Qty, and the
   unit of the size (oz, lb, g...) is NOT converted — only N matters. Copy
   that column's text exactly into `pack_size`; leave `pack_size` null when
   the invoice has no such column.
   Example: printed Qty 1, Pack Size "12/12 oz", Weight 5.40, unit price
   53.90, extended 53.90 → `quantity`=12, `cases`=1, `pack_size`="12/12 oz"
   (the Weight column is NOT used: the price is per case, not per weight).
   Don't apply this when the description has no "<N>X<size>" pattern and
   there is no pack column, or when rule 7's weight column already fills
   `quantity` and `cases`.
   Why both numbers matter and neither is "the" answer: the same
   "<N>X<size>" description shows up on invoices whose purchase order
   tracks the item in individual units (where only the multiplied
   `quantity` will match what was ordered) AND on invoices whose purchase
   order tracks it by the case/pack (where only the original, unmultiplied
   `cases` count will match) — there is no way to tell which one applies
   from the invoice image alone, since that depends on how the OTHER
   system (the purchase order) recorded the item, not on anything printed
   here. Extract both numbers faithfully and let the downstream
   reconciliation — which does have the purchase order — pick the right
   one.

Return this exact JSON structure (use null for unknown fields):
{
  "invoice_number": null,
  "invoice_date": null,
  "invoice_date_raw": null,
  "due_date": null,
  "due_date_raw": null,
  "currency": "USD",
  "subtotal": null,
  "tax_amount": null,
  "total_amount": null,
  "supplier_name": null,
  "supplier_address": null,
  "supplier_phone": null,
  "supplier_email": null,
  "supplier_tax_id": null,
  "client_name": null,
  "client_address": null,
  "client_tax_id": null,
  "items": [
    {
      "item_order": 1,
      "description": null,
      "item_code": null,
      "upc": null,
      "pack_size": null,
      "quantity": null,
      "unit": null,
      "unit_price": null,
      "total_price": null,
      "cases": null,
      "handwritten_code": null,
      "handwritten_notes": null
    }
  ],
  "general_handwritten_notes": null,
  "reading_confidence": 95,
  "reading_status": "success",
  "reading_notes": null
}
"""


# ---------------------------------------------------------------------------
# Funções de leitura
# ---------------------------------------------------------------------------

def _repair_truncated_json(s: str) -> dict:
    """
    Tenta reparar JSON truncado fechando colchetes/chaves abertas.
    Usado quando max_tokens é atingido no meio do JSON.
    """
    # Conta abertura/fechamento de estruturas
    stack = []
    in_string = False
    escape = False

    for ch in s:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack and stack[-1] == ch:
                stack.pop()

    # Fecha tudo o que ficou aberto
    repaired = s.rstrip().rstrip(",") + "".join(reversed(stack))

    try:
        return json.loads(repaired)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"JSON truncado e não foi possível reparar: {exc}. "
            f"Tente aumentar max_tokens ou simplificar o prompt."
        ) from exc


def _media_type(suffix: str) -> tuple[str, str]:
    """Returns (block_type, media_type) for a given file extension."""
    mapping = {
        ".pdf":  ("document", "application/pdf"),
        ".jpg":  ("image",    "image/jpeg"),
        ".jpeg": ("image",    "image/jpeg"),
        ".png":  ("image",    "image/png"),
        ".webp": ("image",    "image/webp"),
        ".gif":  ("image",    "image/gif"),
    }
    result = mapping.get(suffix.lower())
    if result is None:
        raise ValueError(f"Formato não suportado: '{suffix}'. Use PDF, JPG, PNG, WEBP ou GIF.")
    return result


def read_invoice(file_path: Path, api_key: str, model: str = "claude-sonnet-4-6") -> InvoiceData:
    """
    Lê um arquivo de invoice (PDF ou imagem) e retorna os dados estruturados
    extraídos pelo Claude Vision (claude-opus-4-8).

    Args:
        file_path: Caminho para o arquivo (PDF, JPG, PNG, etc.)
        api_key:   Chave da API Anthropic — lida do profile como 'schiavon_key_vision'

    Returns:
        InvoiceData com todos os campos extraídos e indicador de confiança.
    """
    client = anthropic.Anthropic(api_key=api_key)

    block_type, media_type = _media_type(file_path.suffix)
    file_b64 = base64.standard_b64encode(file_path.read_bytes()).decode("utf-8")

    content_block: dict = {
        "type": block_type,
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": file_b64,
        },
    }

    response = client.messages.create(
        model=model,
        max_tokens=8192,
        thinking={"type": "disabled"},
        messages=[
            {
                "role": "user",
                "content": [
                    content_block,
                    {"type": "text", "text": _PROMPT},
                ],
            }
        ],
    )

    # Avisa se o modelo parou por limite de tokens (JSON provavelmente truncado)
    if response.stop_reason == "max_tokens":
        log.warning(
            "stop_reason=max_tokens - JSON pode estar truncado (%s tokens de saida)",
            response.usage.output_tokens,
        )

    raw = response.content[0].text

    # Extrai o JSON da resposta (remove eventual texto ao redor)
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError(f"Resposta da API não contém JSON válido: {raw[:200]}")

    json_str = match.group()

    # Tenta parsear; se falhar por truncamento, tenta fechar o JSON
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        data = _repair_truncated_json(json_str)

    # Preços por milhão de tokens (USD) — preço cheio para chamadas síncronas
    usage = response.usage
    _PRICES_SYNC = {
        "claude-haiku-4-5":  (1.00,  5.00),
        "claude-sonnet-4-6": (3.00, 15.00),
        "claude-opus-4-8":   (5.00, 25.00),
    }
    price_in, price_out = _PRICES_SYNC.get(model, (5.00, 25.00))
    cost = (usage.input_tokens * price_in + usage.output_tokens * price_out) / 1_000_000

    data["model_ai"] = model
    data["cost_read"] = round(cost, 6)

    return InvoiceData(**data)


def read_invoices_from_dir(
    directory: Path,
    api_key: str,
    extensions: tuple[str, ...] = (".pdf", ".jpg", ".jpeg", ".png"),
    model: str = "claude-sonnet-4-6",
) -> list[tuple[Path, InvoiceData]]:
    """
    Lê todos os invoices de um diretório.
    Retorna lista de (caminho_do_arquivo, dados_extraídos).
    """
    results = []
    files = [f for f in directory.iterdir() if f.suffix.lower() in extensions]

    for file_path in sorted(files):
        log.info("Lendo: %s", file_path.name)
        try:
            data = read_invoice(file_path, api_key, model)
            log.info(
                "confianca=%.0f%% status=%s itens=%s",
                data.reading_confidence, data.reading_status, len(data.items),
            )
            results.append((file_path, data))
        except Exception as exc:
            log.error("Erro: %s", exc)

    return results


# ---------------------------------------------------------------------------
# Batch API — 50% de desconto, processamento paralelo
# ---------------------------------------------------------------------------

_PRICES = {
    "claude-haiku-4-5":  (0.50,  2.50),   # 50% do preço normal
    "claude-sonnet-4-6": (1.50,  7.50),
    "claude-opus-4-8":   (2.50, 12.50),
}


class BatchItem(NamedTuple):
    file_path: Path
    config_id: int          # id da loja em dim_loja
    config_name: str


def _build_content_block(file_path: Path) -> dict:
    block_type, media_type = _media_type(file_path.suffix)
    file_b64 = base64.standard_b64encode(file_path.read_bytes()).decode("utf-8")
    return {
        "type": block_type,
        "source": {"type": "base64", "media_type": media_type, "data": file_b64},
    }


def _parse_invoice_json(raw: str, usage, model: str) -> InvoiceData:
    """Extrai e valida o JSON retornado pelo modelo."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError(f"Resposta sem JSON válido: {raw[:200]}")
    json_str = match.group()
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        data = _repair_truncated_json(json_str)

    price_in, price_out = _PRICES.get(model, (2.50, 12.50))
    cost = (usage.input_tokens * price_in + usage.output_tokens * price_out) / 1_000_000
    data["model_ai"] = model
    data["cost_read"] = round(cost, 6)
    return InvoiceData(**data)


def submit_batch(
    items: list[BatchItem],
    api_key: str,
    model: str = "claude-sonnet-4-6",
) -> tuple[str, dict[str, BatchItem]]:
    """
    Envia todos os arquivos para o Batch API em uma única requisição.

    Retorna:
        batch_id    — ID do batch para consulta posterior
        id_map      — mapeamento custom_id → BatchItem
    """
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    client = anthropic.Anthropic(api_key=api_key)
    id_map: dict[str, BatchItem] = {}
    requests: list[Request] = []

    for idx, item in enumerate(items):
        custom_id = f"inv_{idx:04d}"
        id_map[custom_id] = item

        requests.append(Request(
            custom_id=custom_id,
            params=MessageCreateParamsNonStreaming(
                model=model,
                max_tokens=8192,
                thinking={"type": "disabled"},
                messages=[{
                    "role": "user",
                    "content": [
                        _build_content_block(item.file_path),
                        {"type": "text", "text": _PROMPT},
                    ],
                }],
            ),
        ))

    log.info("Enviando %s arquivo(s) para o Batch API...", len(requests))
    batch = client.messages.batches.create(requests=requests)
    log.info("Batch criado: %s", batch.id)
    return batch.id, id_map


def wait_for_batch(
    batch_id: str,
    api_key: str,
    poll_interval: int = 30,
    max_wait_seconds: int = 86_400,
) -> None:
    """Aguarda o batch terminar, exibindo progresso a cada intervalo.

    Lança TimeoutError se max_wait_seconds for atingido (padrão: 24h).
    """
    client = anthropic.Anthropic(api_key=api_key)
    log.info("Aguardando conclusao do batch %s...", batch_id)
    start = time.time()

    while True:
        elapsed = int(time.time() - start)
        if elapsed > max_wait_seconds:
            raise TimeoutError(
                f"Batch {batch_id} não concluiu em {max_wait_seconds}s — "
                "verifique o status no painel da Anthropic."
            )

        batch = client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        log.info(
            "[%ss] status=%s processando=%s concluidos=%s erros=%s",
            elapsed, batch.processing_status, counts.processing, counts.succeeded, counts.errored,
        )

        if batch.processing_status == "ended":
            break
        time.sleep(poll_interval)


def collect_batch_results(
    batch_id: str,
    api_key: str,
    id_map: dict[str, BatchItem],
    model: str = "claude-sonnet-4-6",
) -> list[tuple[BatchItem, InvoiceData | None, str | None]]:
    """
    Coleta os resultados do batch.

    Retorna lista de (BatchItem, InvoiceData | None, erro | None).
    """
    client = anthropic.Anthropic(api_key=api_key)
    results: list[tuple[BatchItem, InvoiceData | None, str | None]] = []

    for result in client.messages.batches.results(batch_id):
        item = id_map[result.custom_id]

        if result.result.type == "succeeded":
            msg = result.result.message
            raw = next((b.text for b in msg.content if b.type == "text"), "")
            try:
                invoice = _parse_invoice_json(raw, msg.usage, model)
                results.append((item, invoice, None))
            except Exception as exc:
                results.append((item, None, str(exc)))
        else:
            err = getattr(result.result, "error", result.result.type)
            results.append((item, None, str(err)))

    return results

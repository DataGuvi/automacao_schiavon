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
You are an expert at reading vendor invoices (digital PDFs and scanned paper).

Extract ALL visible information and return ONLY a valid JSON object
(no markdown, no explanation - raw JSON only).

Rules:

1. HANDWRITING - three kinds, each in a DIFFERENT field:
   - A short handwritten NUMBER on or right below a line, alone, that looks like an
     item code (e.g. "40102"; not a price or quantity fix): `handwritten_code` of that
     item, digits only. It is the buyer's own catalog code - do not skip it as noise.
   - Handwriting next to ONE specific line (quantity/price corrections, returns,
     approval stamps, initials): free text in `handwritten_notes` of that item.
     The word INSUMO beside a line, or a brace/bracket "}" in the right margin with
     INSUMO, goes in `handwritten_notes` of EVERY covered line: all lines from the
     brace's top tip to its bottom tip, even though the word is written only once
     (usually mid-brace). Ignore "Substitution" sub-rows. A page can have several
     braces, and a brace can continue on the next page. A lone diagonal slash or tick
     without the word is NOT insumo: leave that line's `handwritten_notes` null.
   - Handwriting NOT tied to a line (margin, below the item table, near the signature
     or the Open Invoices list). Decide in THIS order:
     1. You can read the word INSUMO, even split or with a destination ("Insumo",
        "IN SUMO", "Insumo Burger", "INSUNO"): copy it exactly as read into
        `general_handwritten_notes`, with NO prefix and NO flag.
     2. You cannot read INSUMO, but it looks like a near-miss of it or a word that is
        not a person's name ("INS BURGER", "INV BURDER", "INSUM", "IN5UMO"): write
        `INSUMO_INCERTO: <exactly what you see>` in `general_handwritten_notes`.
        Doubt is reported, not dropped.
     3. Only a signature scribble, initials, numbers or a stamp: leave
        `general_handwritten_notes` null.
     NEVER guess, complete or correct the word. The flag is ONLY for case 2. Anything
     else (signature, initials, "DONE", stamps, comments) goes briefly in
     `reading_notes`, never in `general_handwritten_notes`.

2. DATES - ISO 8601 (YYYY-MM-DD) in `invoice_date` / `due_date`. These are US
   invoices: "10/05/26" or "10/05/2026" is MONTH/DAY/YEAR (October 5, 2026). Also copy
   each date EXACTLY as printed into `invoice_date_raw` / `due_date_raw`.

3. AMOUNTS - separate `subtotal`, `tax_amount` and `total_amount`.

4. LINE ITEMS - every row, in order. Copy `quantity`, `unit_price` and `total_price`
   EXACTLY as printed. Never multiply, convert units or recompute one from another
   (a pack such as "12x500g" does not change the quantity).

5. ITEM CODE / UPC - `item_code` is the supplier's own SKU column (headers like "Item",
   "Product", "SKU"), not the description. `upc` is the barcode (digits only), in its
   own column or beside the description. When the ONLY code on the line is a UPC, put
   it in `upc` and leave `item_code` null; leave both null when there is none. A
   leading "#" column is the row number (`item_order`), never `item_code`; when a line
   has both "#" and SKU/Item/Product, `item_code` comes from SKU/Item/Product.

6. READING CONFIDENCE - integer 0-100:
   90-100: all clear | 70-89: minor issues | 50-69: significant issues | 0-49: major problems

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
  "items": [
    {
      "item_order": 1,
      "description": null,
      "item_code": null,
      "upc": null,
      "quantity": null,
      "unit": null,
      "unit_price": null,
      "total_price": null,
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


# Modelos que aceitam `thinking: disabled`; os mais novos (5.x) exigem thinking adaptativo.
_MODELOS_SEM_THINKING = {"claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-8"}


def _config_modelo(model: str) -> dict:
    """`max_tokens` e thinking da chamada. Modelo novo pensa (adaptativo, esforco medio;
    o pensamento conta nos tokens, entao o teto sobe) e a resposta vem apos o bloco de thinking."""
    if model in _MODELOS_SEM_THINKING:
        return {"max_tokens": 8192, "thinking": {"type": "disabled"}}
    return {
        "max_tokens": 16000,
        "thinking": {"type": "adaptive", "display": "omitted"},
        "output_config": {"effort": "medium"},
    }


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
        **_config_modelo(model),
        cache_control={"type": "ephemeral"},
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

    raw = next((b.text for b in response.content if b.type == "text"), "")

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
                **_config_modelo(model),
                cache_control={"type": "ephemeral"},
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

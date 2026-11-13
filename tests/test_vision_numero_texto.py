"""spec-vision-numero-texto: texto em campo numerico nao derruba a nota."""
from commons.vision.schema import InvoiceData


def test_texto_em_campo_numerico_vira_numero_ou_none():
    inv = InvoiceData.model_validate({
        "reading_confidence": 80,
        "total_amount": "$1,541.30",
        "items": [
            {"description": "a", "unit_price": "*NOT AVAIL", "total_price": "1,541.30", "quantity": 2},
        ],
    })
    item = inv.items[0]
    assert inv.total_amount == 1541.3
    assert item.unit_price is None and item.total_price == 1541.3 and item.quantity == 2


def test_schema_enviado_ao_modelo_continua_number():
    props = InvoiceData.model_json_schema()["$defs"]["InvoiceItem"]["properties"]
    assert {"type": "number"} in props["unit_price"]["anyOf"]

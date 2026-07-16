from __future__ import annotations


def invoice_total(items: list[dict], tax_rate: float = 0.0) -> int:
    subtotal = sum(item["price"] for item in items)
    return int(subtotal * (1 + tax_rate))

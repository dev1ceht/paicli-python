import pytest

from src.invoice import invoice_total


def test_quantity_tax_rounding_and_validation():
    items = [{"price": 12.50, "quantity": 2}, {"price": 3.333, "quantity": 3}]
    assert invoice_total(items, tax_rate=0.0825) == 37.89
    assert invoice_total([{"price": 4.25}], tax_rate=0) == 4.25
    with pytest.raises(ValueError):
        invoice_total([{"price": 2, "quantity": -1}])

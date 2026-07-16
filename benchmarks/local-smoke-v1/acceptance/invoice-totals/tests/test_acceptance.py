import pytest

from src.invoice import invoice_total


def test_invoice_contract_edges():
    assert invoice_total([]) == 0.0
    assert invoice_total([{"price": 2.345, "quantity": 2}], tax_rate=0.1) == 5.16
    assert invoice_total([{"price": 10, "quantity": 0}, {"price": 1.25}]) == 1.25
    assert isinstance(invoice_total([{"price": 2}]), float)
    with pytest.raises(ValueError):
        invoice_total([{"price": 2, "quantity": -0.5}])

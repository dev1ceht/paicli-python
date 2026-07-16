import pytest

from src.text_tools import normalize_username


def test_normalizes_and_rejects_blank_usernames():
    assert normalize_username("  Alice.Smith  ") == "alice.smith"
    assert normalize_username("Ada   Lovelace") == "ada-lovelace"
    with pytest.raises(ValueError, match="blank"):
        normalize_username("   ")

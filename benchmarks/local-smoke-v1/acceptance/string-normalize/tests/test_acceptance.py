import pytest

from src.text_tools import normalize_username


def test_normalizes_whitespace_boundaries():
    assert normalize_username("already-normal") == "already-normal"
    assert normalize_username("\tGrace\n Hopper\t") == "grace-hopper"
    with pytest.raises(ValueError, match="blank"):
        normalize_username("\t\n  ")

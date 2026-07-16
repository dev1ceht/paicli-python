from pathlib import Path

from app import get


def test_health_and_index_routes():
    assert get("/health") == ({"status": "ok"}, 200)
    assert get("/") == ({"status": "running"}, 200)


def test_public_tests_cover_health_route():
    public_test = Path("tests/test_public.py").read_text(encoding="utf-8")
    assert "/health" in public_test
    assert "get(" in public_test

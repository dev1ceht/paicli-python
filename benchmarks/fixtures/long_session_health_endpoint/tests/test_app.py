from app import get


def test_index():
    assert get("/")[0] == {"status": "running"}

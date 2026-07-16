from app import get


def test_index_is_running():
    assert get("/") == ({"status": "running"}, 200)

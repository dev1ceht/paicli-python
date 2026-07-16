from pathlib import Path

from src.main import greeting
from src.profile import get_user_name
from src.report import render_user


def test_new_name_is_used_everywhere_and_preserves_behavior():
    assert get_user_name({"name": " ada "}) == "Ada"
    assert greeting({"name": "grace"}) == "Hello, Grace"
    assert render_user({"name": "alan"}) == "User: Alan"
    source = "\n".join(path.read_text(encoding="utf-8") for path in Path("src").glob("*.py"))
    assert "getUserName" not in source

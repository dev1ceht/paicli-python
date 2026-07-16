from src.main import greeting
from src.profile import getUserName
from src.report import render_user


def test_existing_name_behavior():
    assert getUserName({"name": " ada "}) == "Ada"
    assert greeting({"name": "grace"}) == "Hello, Grace"
    assert render_user({"name": "alan"}) == "User: Alan"

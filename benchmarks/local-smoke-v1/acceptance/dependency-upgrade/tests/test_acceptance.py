import ast
from pathlib import Path


def test_all_request_defaults_are_safe():
    assert Path("requirements.txt").read_text(encoding="utf-8").strip() == "requests==2.31"
    assert "trust_env = False" in Path("src/client.py").read_text(encoding="utf-8")
    for path in (Path("src/api.py"), Path("src/fetcher.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        request_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "requests"
            and node.func.attr == "get"
        ]
        assert len(request_calls) == 1
        keywords = {keyword.arg: keyword.value for keyword in request_calls[0].keywords}
        assert "timeout" in keywords
        timeout = ast.literal_eval(keywords["timeout"])
        assert isinstance(timeout, int | float) and not isinstance(timeout, bool) and timeout > 0
        if "verify" in keywords:
            assert ast.literal_eval(keywords["verify"]) is not False

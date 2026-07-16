from pathlib import Path


def test_dependency_and_session_defaults_are_hardened():
    assert Path("requirements.txt").read_text(encoding="utf-8").strip() == "requests==2.31"
    assert "trust_env = False" in Path("src/client.py").read_text(encoding="utf-8")

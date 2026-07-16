from pathlib import Path


def test_auth_config_uses_schema_two():
    text = Path("services/auth/config.yaml").read_text(encoding="utf-8")
    assert "schema_version: 2" in text
    assert "service: auth" in text

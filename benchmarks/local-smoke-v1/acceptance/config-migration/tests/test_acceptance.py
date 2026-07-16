from pathlib import Path


def test_every_service_is_migrated_without_renaming():
    expected = {"api", "auth", "gateway", "worker"}
    paths = list(Path("services").glob("*/config.yaml"))
    assert {path.parent.name for path in paths} == expected
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "schema_version: 2" in text
        assert "schema_version: 1" not in text
        assert f"service: {path.parent.name}" in text

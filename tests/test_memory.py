from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from paicli.memory import MemoryManager, estimate_tokens


def test_memory_manager_saves_java_style_project_and_global_entries(tmp_path):
    storage = tmp_path / "long_term_memory.json"
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()

    manager = MemoryManager(storage, project_path=project)
    project_id = manager.save("Chrome prefers shared login state")
    global_id = manager.save("Always answer in Chinese", scope="global")
    manager.save("Other project only", scope="project", project_path=other)

    raw = json.loads(storage.read_text(encoding="utf-8"))
    assert {item["id"] for item in raw} == {project_id, global_id, raw[2]["id"]}
    assert raw[0]["type"] == "FACT"
    assert raw[0]["metadata"]["scope"] == "project"
    assert raw[0]["metadata"]["project"] == str(project.resolve())
    assert raw[0]["tokenCount"] == estimate_tokens("Chrome prefers shared login state")
    assert raw[1]["metadata"]["scope"] == "global"
    assert "project" not in raw[1]["metadata"]

    visible = manager.list()

    assert [entry.content for entry in visible] == [
        "Chrome prefers shared login state",
        "Always answer in Chinese",
    ]


def test_memory_manager_searches_visible_entries_with_relevance_and_budget(tmp_path):
    storage = tmp_path / "long_term_memory.json"
    project = tmp_path / "project"
    project.mkdir()
    manager = MemoryManager(storage, project_path=project)
    older = datetime.now(UTC) - timedelta(days=2)
    manager.save("Chrome browser login reuse is allowed")
    manager.save("Chrome login exact preference", timestamp=older)
    manager.save("Unrelated pytest preference", scope="global")

    results = manager.search("Chrome login", limit=5)
    context = manager.build_context_for_query("Chrome login", max_tokens=20)

    assert [entry.content for entry in results[:2]] == [
        "Chrome login exact preference",
        "Chrome browser login reuse is allowed",
    ]
    assert "## 相关长期记忆" in context
    assert "[FACT] Chrome login exact preference" in context
    assert "Unrelated pytest preference" not in context


def test_memory_manager_delete_clear_and_deduplicate(tmp_path):
    storage = tmp_path / "long_term_memory.json"
    project = tmp_path / "project"
    project.mkdir()
    manager = MemoryManager(storage, project_path=project)
    memory_id = manager.save("Use pytest for verification")
    duplicate_id = manager.save("Use pytest for verification")

    assert duplicate_id == memory_id
    assert len(manager.list()) == 1
    assert manager.delete(memory_id) is True
    assert manager.delete(memory_id) is False

    manager.save("Project fact")
    manager.save("Global fact", scope="global")

    assert manager.clear() == 2
    assert manager.list() == []

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

from paicli.context.telemetry import current_context_scope, use_context_scope
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


def test_memory_manager_applies_and_rejects_pending_changes(tmp_path):
    storage = tmp_path / "long_term_memory.json"
    project = tmp_path / "project"
    project.mkdir()
    manager = MemoryManager(storage, project_path=project)
    old_id = manager.save("Use pytest for verification")

    change = manager.propose_change(
        operation="replace",
        target_memory_ids=[old_id],
        proposed_content="Use unittest for verification",
        reason="The test framework was migrated.",
        source_fact="Use unittest for verification",
    )

    assert [item.id for item in manager.list_pending()] == [change.id]
    assert [item.content for item in manager.list()] == ["Use pytest for verification"]
    applied_id = manager.apply_pending(change.id)
    assert [item.content for item in manager.list()] == ["Use unittest for verification"]
    assert applied_id
    assert manager.list_pending() == []

    rejected = manager.propose_change(
        operation="retire",
        target_memory_ids=[applied_id],
        proposed_content="",
        reason="No longer applicable.",
        source_fact="Forget the test framework preference",
    )
    assert manager.reject_pending(rejected.id)
    assert [item.content for item in manager.list()] == ["Use unittest for verification"]


def test_memory_classification_is_excluded_from_context_telemetry(tmp_path):
    scopes: list[str | None] = []

    class ClassificationClient:
        async def chat(self, messages, tools, *, system_prompt):
            del messages, tools, system_prompt
            scopes.append(current_context_scope())
            yield {
                "type": "text_delta",
                "text": '{"relationship":"independent"}',
            }

    manager = MemoryManager(tmp_path / "memory.json", project_path=tmp_path)
    manager.save("Use pytest for verification")

    async def run():
        with use_context_scope("agent"):
            await manager.save_with_classification(
                "Use pytest for all verification",
                scope="project",
                llm_client=ClassificationClient(),
            )

    asyncio.run(run())

    assert scopes == [None]

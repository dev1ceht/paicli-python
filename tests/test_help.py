from __future__ import annotations

from paicli.entrypoints.repl import SLASH_COMMANDS, help_text
from paicli.render.tui_app import PaiCliApp


def test_help_text_lists_commands_with_descriptions():
    text = help_text()

    assert "/model - 查看当前模型" in text
    assert "/plan <任务内容> - 直接用计划模式执行这条任务" in text
    assert "/browser connect <port> - 旧式 CDP 端口连接" in text
    assert "/task add <任务内容> - 提交后台任务" in text
    assert "/task retry <task_id|N|latest> - 重试失败的后台任务" in text
    assert "/mcp restart <name> - 重启 MCP server" in text
    assert "/wechat" not in text


def test_help_lists_every_slash_command_in_repl_and_tui():
    repl_help = help_text()
    tui_help = PaiCliApp._help_text(None)

    assert tui_help == repl_help
    for command in SLASH_COMMANDS:
        assert command in repl_help
        assert command in tui_help


def test_help_explains_durable_sessions_and_background_tasks():
    text = help_text()

    assert "/session resume <session-id> - 恢复指定会话" in text
    assert "/session restore <session-id> - 从回收站恢复会话" in text
    assert (
        "持久化：完整 Session 保存在 ~/.paicli/sessions/*.jsonl，"
        "后台任务状态保存在 ~/.paicli/runtime/tasks.db"
    ) in text
    assert "后台任务是提交会话的 child Session" in text
    assert "后台任务的对话历史、工具调用状态和待审批状态会持久化" in text
    assert "中断的运行中任务不会自动重试，可使用 /task retry" in text
    assert ("状态：queued | running | waiting_approval | completed | failed | canceled") in text

from __future__ import annotations

from paicli.entrypoints.repl import help_text


def test_help_text_lists_commands_with_descriptions():
    text = help_text()

    assert "/model - 查看当前模型" in text
    assert "/plan <任务内容> - 直接用计划模式执行这条任务" in text
    assert "/browser connect <port> - 旧式 CDP 端口连接" in text
    assert "/task add <任务内容> - 提交后台任务" in text
    assert "/mcp restart <name> - 重启 MCP server" in text
    assert "/wechat" not in text

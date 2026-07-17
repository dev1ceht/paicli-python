"""Shared rendering utilities.

Centralises tool labels, formatting helpers, cost estimation, the ASCII logo,
path shortening, and the LCS diff algorithm that are used by both the Rich
console renderer and the Textual TUI widgets.
"""

from __future__ import annotations

import json
import os
from typing import Any

# ---------------------------------------------------------------------------
# NO_COLOR support (https://no-color.org/)
# ---------------------------------------------------------------------------

NO_COLOR = os.environ.get("NO_COLOR") is not None


# ---------------------------------------------------------------------------
# Tool call labels (Chinese + emoji, matching Java ToolCallRenderer)
# ---------------------------------------------------------------------------

TOOL_LABELS: dict[str, tuple[str, str]] = {
    # (emoji_label, key_param_name)
    "read_file": ("\U0001f4d6 \u8bfb\u53d6", "path"),
    "write_file": ("\u270f\ufe0f \u5199\u5165", "path"),
    "edit_file": ("\u270f\ufe0f \u7f16\u8f91", "path"),
    "apply_patch": ("\U0001fa79 \u5e94\u7528\u8865\u4e01", "patch"),
    "list_dir": ("\U0001f4c2 \u5217\u51fa\u76ee\u5f55", "path"),
    "execute_command": ("\u26a1 \u6267\u884c\u547d\u4ee4", "command"),
    "bash": ("\u26a1 \u6267\u884c\u547d\u4ee4", "command"),
    "create_project": ("\U0001f3d7\ufe0f \u521b\u5efa\u9879\u76ee", "name"),
    "grep_code": ("\U0001f50d \u641c\u7d22\u4ee3\u7801", "query"),
    "grep": ("\U0001f50d \u641c\u7d22\u4ee3\u7801", "query"),
    "search_code": ("\U0001f50d \u641c\u7d22\u4ee3\u7801", "query"),
    "glob": ("\U0001f4c2 \u6587\u4ef6\u5339\u914d", "pattern"),
    "glob_files": ("\U0001f4c2 \u6587\u4ef6\u5339\u914d", "pattern"),
    "web_search": ("\U0001f310 \u8054\u7f51\u641c\u7d22", "query"),
    "web_fetch": ("\U0001f4f0 \u6293\u53d6\u7f51\u9875", "url"),
    "save_memory": ("\U0001f4be \u4fdd\u5b58\u8bb0\u5fc6", "fact"),
    "load_skill": ("\U0001f3af \u52a0\u8f7d\u6280\u80fd", "name"),
    "revert_turn": ("\u23ea \u56de\u9000\u5feb\u7167", "id"),
    "browser_connect": ("\U0001f310 \u8fde\u63a5\u6d4f\u89c8\u5668", ""),
    "browser_disconnect": ("\U0001f310 \u65ad\u5f00\u6d4f\u89c8\u5668", ""),
    "browser_status": ("\U0001f310 \u6d4f\u89c8\u5668\u72b6\u6001", ""),
    "browser_tabs": ("\U0001f310 \u6d4f\u89c8\u5668\u6807\u7b7e", ""),
}


def tool_label(name: str, payload: dict[str, Any] | Any) -> str:
    """Build a human-readable label for a tool call, matching Java style."""
    info = TOOL_LABELS.get(name)
    if info:
        label, key_param = info
        if key_param and isinstance(payload, dict):
            value = payload.get(key_param, "")
            if value:
                short = str(value)[:60]
                return f'{label}("{short}")'
        return label
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3:
            return f"\U0001f50c MCP {parts[1]}.{parts[2]}"
    return f"\U0001f527 {name}"


# ---------------------------------------------------------------------------
# Formatting helpers (matching Java StatusInfo / AnsiSeq formatting)
# ---------------------------------------------------------------------------


def format_tokens(count: int) -> str:
    """Format token count: >=1M -> '1.2M', >=1k -> '51.0k', else plain."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def format_elapsed(seconds: float) -> str:
    """Format elapsed time: <1s -> '250ms', >=1s -> '1.5s'."""
    if seconds < 1.0:
        return f"{int(seconds * 1000)}ms"
    return f"{seconds:.1f}s"


def format_cost(cost_yuan: float) -> str:
    """Format cost: '¥0.0123'. Returns empty string if zero."""
    if cost_yuan <= 0.0001:
        return ""
    return f"\u00a5{cost_yuan:.4f}"


# ---------------------------------------------------------------------------
# Cost estimation (simplified per-1k-token pricing)
# ---------------------------------------------------------------------------

_PRICE_PER_1K_INPUT: dict[str, float] = {
    "deepseek": 0.00014,
    "openai": 0.0025,
    "glm": 0.001,
    "kimi": 0.001,
    "step": 0.001,
    "qwen": 0.001,
    "dashscope": 0.001,
}

_PRICE_PER_1K_OUTPUT: dict[str, float] = {
    "deepseek": 0.00028,
    "openai": 0.01,
    "glm": 0.002,
    "kimi": 0.002,
    "step": 0.002,
    "qwen": 0.002,
    "dashscope": 0.002,
}


def estimate_cost(provider: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in yuan for given token counts and provider."""
    in_price = _PRICE_PER_1K_INPUT.get(provider, 0.001)
    out_price = _PRICE_PER_1K_OUTPUT.get(provider, 0.002)
    return (input_tokens / 1000) * in_price + (output_tokens / 1000) * out_price


# ---------------------------------------------------------------------------
# Logo
# ---------------------------------------------------------------------------

PI_LOGO = (
    "\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2588",
    "  \u2588\u2588    \u2588\u2588  ",
    "  \u2588\u2588    \u2588\u2588  ",
    "  \u2588\u2588    \u2588\u2588  ",
    "  \u2588\u2588    \u2588\u2588  ",
    "  \u2588\u2588    \u2588\u2588  ",
)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def shorten_home(path: str) -> str:
    """Shorten ``~/…`` for display."""
    home = str(os.path.expanduser("~"))
    if path == home:
        return "~"
    prefix = home + os.sep
    if path.startswith(prefix):
        return "~/" + path[len(prefix):]
    return path


# ---------------------------------------------------------------------------
# Payload formatting
# ---------------------------------------------------------------------------


def format_payload(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except TypeError:
        return str(payload)


# ---------------------------------------------------------------------------
# Simple LCS diff (matching Java InlineDiffRenderer)
# ---------------------------------------------------------------------------

_CONTEXT_LINES = 2


def diff_ops(
    before: list[str], after: list[str]
) -> list[tuple[str, str]]:
    """Compute diff operations: list of (op, line) tuples.

    op is '+', '-', or ' ' (context).
    Uses naive LCS then context trimming.
    """
    n, m = len(before), len(after)

    # LCS table
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if before[i - 1] == after[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])

    # Backtrack to raw ops
    raw_ops: list[tuple[str, str]] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and before[i - 1] == after[j - 1]:
            raw_ops.append(("=", before[i - 1]))
            i -= 1
            j -= 1
        elif j > 0 and (i == 0 or dp[i][j - 1] >= dp[i - 1][j]):
            raw_ops.append(("+", after[j - 1]))
            j -= 1
        else:
            raw_ops.append(("-", before[i - 1]))
            i -= 1
    raw_ops.reverse()

    # Group into hunks with context
    return _group_hunks(raw_ops)


def _group_hunks(
    raw_ops: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Group raw diff ops into hunks with context lines."""
    result: list[tuple[str, str]] = []
    n = len(raw_ops)
    i = 0
    while i < n:
        op, line = raw_ops[i]
        if op != "=":
            result.append(("+" if op == "+" else "-", line))
            i += 1
            continue

        # Context line: check if near a change
        # Find the distance to the nearest non-equal op
        nearest_change = n  # default: no change ahead
        for k in range(i + 1, min(i + _CONTEXT_LINES * 2 + 2, n)):
            if raw_ops[k][0] != "=":
                nearest_change = k
                break

        # Also check backward
        nearest_change_back = -1
        for k in range(i - 1, max(i - _CONTEXT_LINES - 1, -1), -1):
            if raw_ops[k][0] != "=":
                nearest_change_back = k
                break

        dist_to_change = nearest_change - i
        dist_from_change = i - nearest_change_back

        if dist_to_change <= _CONTEXT_LINES or dist_from_change <= _CONTEXT_LINES:
            result.append((" ", line))
        i += 1

    return result

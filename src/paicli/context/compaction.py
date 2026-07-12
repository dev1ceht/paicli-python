"""结构化压缩模块

阶段四：当压力达到 tier3_summary，触发压缩。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from paicli.context.token_estimator import estimate_tokens
from paicli.llm.base import LlmClient
from paicli.types import Message

# 压缩摘要的最大字符数
MAX_SUMMARY_CHARS = 2000

# 保护最近 N 个 turn 不压缩
PROTECTED_TURNS = 2

# LLM 摘要的输入限制
LLM_SUMMARY_INPUT_LIMIT = 20_000


@dataclass
class CompactionResult:
    """压缩结果"""

    summary: str
    compacted_items: int
    protected_items: int
    used_llm: bool
    llm_usage: dict[str, int] = field(default_factory=dict)

    @property
    def summary_tokens(self) -> int:
        """摘要的 token 数"""
        return estimate_tokens(self.summary)


@dataclass
class DeltaItem:
    """待压缩的历史项"""

    turn_id: int
    role: str
    content: str
    tool_call_id: str | None = None

    @property
    def is_tool_result(self) -> bool:
        """是否是工具结果"""
        return self.role == "tool"

    @property
    def is_user_message(self) -> bool:
        """是否是用户消息"""
        return self.role == "user"

    @property
    def is_assistant_message(self) -> bool:
        """是否是助手消息"""
        return self.role == "assistant"


def group_messages_by_turn(messages: list[Message]) -> list[list[DeltaItem]]:
    """按 turn 分组消息

    一个 turn 包含：user message -> assistant message -> tool results

    Args:
        messages: 消息列表

    Returns:
        按 turn 分组的列表
    """
    turns: list[list[DeltaItem]] = []
    current_turn: list[DeltaItem] = []
    turn_id = 0

    for msg in messages:
        # 遇到新的 user message 时，开始新的 turn
        if msg.role == "user" and current_turn:
            turns.append(current_turn)
            current_turn = []
            turn_id += 1

        current_turn.append(
            DeltaItem(
                turn_id=turn_id,
                role=msg.role,
                content=msg.content if isinstance(msg.content, str) else str(msg.content),
                tool_call_id=msg.tool_call_id,
            )
        )

    # 添加最后一个 turn
    if current_turn:
        turns.append(current_turn)

    return turns


def extract_delta_items(
    messages: list[Message],
    protected_turns: int = PROTECTED_TURNS,
) -> tuple[list[DeltaItem], list[DeltaItem]]:
    """提取待压缩的 delta items

    保护最近 protected_turns 个 turn 不压缩。

    Args:
        messages: 消息列表
        protected_turns: 保护的 turn 数

    Returns:
        (delta_items, protected_items)
    """
    turns = group_messages_by_turn(messages)

    if len(turns) <= protected_turns:
        # 所有 turn 都受保护
        protected_items = [item for turn in turns for item in turn]
        return [], protected_items

    # 分离 delta 和 protected
    delta_turns = turns[:-protected_turns]
    protected_turn_list = turns[-protected_turns:]

    delta_items = [item for turn in delta_turns for item in turn]
    protected_items = [item for turn in protected_turn_list for item in turn]

    return delta_items, protected_items


async def compact_with_llm(
    delta_items: list[DeltaItem],
    llm_client: LlmClient,
    *,
    prior_summary: str = "",
) -> CompactionResult:
    """使用 LLM 生成结构化摘要

    Args:
        delta_items: 待压缩的历史项
        llm_client: LLM 客户端
        prior_summary: 之前的摘要（用于增量合并）

    Returns:
        压缩结果
    """
    # Only oversized histories need multi-call compaction.
    estimated_input_chars = sum(
        min(len(item.content), 3000 if item.is_tool_result else 2000) + len(item.role) + 4
        for item in delta_items
    )
    if estimated_input_chars > LLM_SUMMARY_INPUT_LIMIT:
        return await _compact_map_reduce(delta_items, llm_client, prior_summary)

    input_text = _build_llm_input(delta_items, prior_summary)

    # 构建 prompt
    system_prompt = _build_compaction_prompt()

    # 调用 LLM
    messages = [Message(role="user", content=input_text)]

    llm_usage = {"input_tokens": 0, "output_tokens": 0}
    response_text = ""

    async for event in llm_client.chat(messages, [], system_prompt=system_prompt):
        if event.get("type") == "text_delta":
            response_text += event.get("text", "")
        elif event.get("type") == "usage":
            usage = event.get("usage", {})
            llm_usage["input_tokens"] = usage.get("input_tokens", 0)
            llm_usage["output_tokens"] = usage.get("output_tokens", 0)

    # 解析响应
    summary = _parse_llm_summary(response_text)

    # 验证摘要
    if not _validate_llm_summary(summary):
        # 摘要无效，回退到确定性摘要
        return deterministic_compact(delta_items, prior_summary, llm_usage=llm_usage)

    return CompactionResult(
        summary=summary,
        compacted_items=len(delta_items),
        protected_items=0,  # 由调用方设置
        used_llm=True,
        llm_usage=llm_usage,
    )


async def _compact_map_reduce(
    delta_items: list[DeltaItem], llm_client: LlmClient, prior_summary: str
) -> CompactionResult:
    chunks = _chunk_delta_items(delta_items)
    usage = {"input_tokens": 0, "output_tokens": 0}
    try:
        summaries = []
        for chunk in chunks:
            summary, item_usage = await _summarize_llm(_build_llm_input(chunk, ""), llm_client)
            if not _validate_llm_summary(summary):
                return deterministic_compact(delta_items, prior_summary, llm_usage=usage)
            summaries.append(summary)
            for key in usage:
                usage[key] += item_usage[key]
        if len(summaries) == 1 and not prior_summary:
            summary = summaries[0]
        else:
            reduction_input = (
                "Prior Summary (merge into your output):\n"
                f"{prior_summary}\n\nChunk Summaries to Merge:\n" + "\n\n---\n\n".join(summaries)
            )
            summary, item_usage = await _summarize_llm(reduction_input, llm_client)
            for key in usage:
                usage[key] += item_usage[key]
        if not _validate_llm_summary(summary):
            return deterministic_compact(delta_items, prior_summary, llm_usage=usage)
        return CompactionResult(
            summary=summary,
            compacted_items=len(delta_items),
            protected_items=0,
            used_llm=True,
            llm_usage=usage,
        )
    except Exception:
        return deterministic_compact(delta_items, prior_summary)


async def _summarize_llm(input_text: str, llm_client: LlmClient) -> tuple[str, dict[str, int]]:
    usage = {"input_tokens": 0, "output_tokens": 0}
    response = ""
    async for event in llm_client.chat(
        [Message(role="user", content=input_text)], [], system_prompt=_build_compaction_prompt()
    ):
        if event.get("type") == "text_delta":
            response += str(event.get("text") or "")
        elif event.get("type") == "usage":
            values = event.get("usage") or {}
            usage["input_tokens"] += int(values.get("input_tokens") or 0)
            usage["output_tokens"] += int(values.get("output_tokens") or 0)
    return _parse_llm_summary(response), usage


def _chunk_delta_items(delta_items: list[DeltaItem]) -> list[list[DeltaItem]]:
    chunks: list[list[DeltaItem]] = [[]]
    size = 0
    for item in delta_items:
        content = item.content[:3000] if item.is_tool_result else item.content[:2000]
        cost = len(content) + len(item.role) + 4
        if chunks[-1] and size + cost > LLM_SUMMARY_INPUT_LIMIT:
            chunks.append([])
            size = 0
        chunks[-1].append(item)
        size += cost
    return [chunk for chunk in chunks if chunk]


def deterministic_compact(
    delta_items: list[DeltaItem],
    prior_summary: str = "",
    *,
    llm_usage: dict[str, int] | None = None,
) -> CompactionResult:
    """确定性摘要（不调用 LLM）

    使用规则提取关键信息。

    Args:
        delta_items: 待压缩的历史项
        prior_summary: 之前的摘要
        llm_usage: LLM 使用量（如果有）

    Returns:
        压缩结果
    """
    parts = []

    # 如果有之前的摘要，先包含
    if prior_summary:
        parts.append(prior_summary)
        parts.append("\n---\nIncremental compacted delta:\n")

    # 提取关键信息
    goal = _extract_goal(delta_items)
    constraints = _extract_constraints(delta_items)
    files_read = _extract_files_read(delta_items)
    files_modified = _extract_files_modified(delta_items)
    key_decisions = _extract_key_decisions(delta_items)
    rejected_paths = _extract_rejected_paths(delta_items)
    last_error = _extract_last_error(delta_items)

    # 构建摘要
    if goal:
        parts.append(f"Goal: {goal}")

    if constraints:
        parts.append(f"Constraints: {', '.join(constraints)}")

    if files_read:
        parts.append(f"Files Read: {', '.join(files_read)}")

    if files_modified:
        parts.append(f"Files Modified: {', '.join(files_modified)}")

    if key_decisions:
        parts.append(f"Key Decisions: {'; '.join(key_decisions)}")

    if rejected_paths:
        parts.append(f"Rejected Paths: {'; '.join(rejected_paths)}")

    if last_error:
        parts.append(f"Last Error: {last_error}")

    parts.append(f"\nCompacted {len(delta_items)} history items.")

    summary = "\n".join(parts)

    # 硬限 2000 字符
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[:MAX_SUMMARY_CHARS]

    return CompactionResult(
        summary=summary,
        compacted_items=len(delta_items),
        protected_items=0,  # 由调用方设置
        used_llm=False,
        llm_usage=llm_usage or {},
    )


def _build_llm_input(delta_items: list[DeltaItem], prior_summary: str) -> str:
    """构建 LLM 输入"""
    parts = []

    # 如果有之前的摘要，包含在输入中
    if prior_summary:
        parts.append("Prior Summary (merge into your output):")
        parts.append(prior_summary)
        parts.append("\n---\n")

    parts.append("New Delta Items to Compact:\n")

    # 添加 delta items
    total_chars = 0
    for item in delta_items:
        content = item.content

        # 截断工具结果
        if item.is_tool_result and len(content) > 3000:
            content = content[:3000] + "\n... [truncated]"

        # 截断用户/助手消息
        if (item.is_user_message or item.is_assistant_message) and len(content) > 2000:
            content = content[:2000] + "\n... [truncated]"

        line = f"[{item.role}] {content}"

        # 检查总长度
        if total_chars + len(line) > LLM_SUMMARY_INPUT_LIMIT:
            parts.append("\n... [more items truncated]")
            break

        parts.append(line)
        total_chars += len(line)

    return "\n".join(parts)


def _build_compaction_prompt() -> str:
    """构建压缩 prompt"""
    return """You are a conversation summarizer. Your task is to create a structured summary of the conversation history.

Output Format (Markdown):
## Goal
What the user is trying to accomplish.

## Constraints
Any constraints or requirements the user specified.

## Files Read
List of files that were read.

## Files Modified
List of files that were modified.

## Completed Operations & Results
Commands, tool actions, and their verified results.

## Key Decisions
Important decisions made and their reasoning.

## Current Workspace State
The relevant implementation and verification state.

## Blockers
Any blockers or unresolved issues.

## Next Steps
What should be done next.

Guidelines:
- Be concise and factual
- Focus on actionable information
- Preserve file paths and technical details exactly
- If there's a Prior Summary, merge the new information into it rather than repeating
- Output only the summary, no explanations
"""


def _parse_llm_summary(response: str) -> str:
    """解析 LLM 响应"""
    # 简单处理：直接使用响应文本
    return response.strip()


def _validate_llm_summary(summary: str) -> bool:
    """验证 LLM 摘要是否有效"""
    # 检查是否包含必要的部分
    has_goal = "Goal" in summary or "goal" in summary
    has_next_steps = "Next Steps" in summary or "next" in summary.lower()

    return has_goal and has_next_steps


def _extract_goal(delta_items: list[DeltaItem]) -> str:
    """提取目标"""
    # 取最近一条 user message
    for item in reversed(delta_items):
        if item.is_user_message:
            # 截取前 200 字符
            goal = item.content[:200]
            if len(item.content) > 200:
                goal += "..."
            return goal
    return ""


def _extract_constraints(delta_items: list[DeltaItem]) -> list[str]:
    """提取约束"""
    constraints = []
    keywords = ["不要", "必须", "只", "don't", "must", "only", "不要", "禁止"]

    for item in delta_items:
        if item.is_user_message:
            # 按句子分割
            sentences = re.split(r"[。！？.!?]", item.content)
            for sentence in sentences:
                sentence = sentence.strip()
                if any(kw in sentence for kw in keywords):
                    constraints.append(sentence)
                    if len(constraints) >= 5:
                        return constraints

    return constraints[:5]


def _extract_files_read(delta_items: list[DeltaItem]) -> list[str]:
    """提取读取的文件"""
    files = []
    for item in delta_items:
        if item.role == "assistant" and "read_file" in item.content:
            # 尝试从 tool call 中提取路径
            match = re.search(r'"path"\s*:\s*"([^"]+)"', item.content)
            if match:
                files.append(match.group(1))
    return list(dict.fromkeys(files))  # 去重并保持顺序


def _extract_files_modified(delta_items: list[DeltaItem]) -> list[str]:
    """提取修改的文件"""
    files = []
    for item in delta_items:
        if item.role == "assistant" and "write_file" in item.content:
            match = re.search(r'"path"\s*:\s*"([^"]+)"', item.content)
            if match:
                files.append(match.group(1))
    return list(dict.fromkeys(files))


def _extract_key_decisions(delta_items: list[DeltaItem]) -> list[str]:
    """提取关键决策"""
    decisions = []
    keywords = ["decided", "选择", "approach", "switched to", "决定", "采用"]

    for item in delta_items:
        if item.is_assistant_message:
            sentences = re.split(r"[。！？.!?]", item.content)
            for sentence in sentences:
                sentence = sentence.strip()
                if any(kw in sentence.lower() for kw in keywords):
                    decisions.append(sentence)
                    if len(decisions) >= 3:
                        return decisions

    return decisions[:3]


def _extract_rejected_paths(delta_items: list[DeltaItem]) -> list[str]:
    """提取被拒绝的路径"""
    rejected = []
    keywords = ["不行", "doesn't work", "reverted", "失败", "错误"]

    for item in delta_items:
        sentences = re.split(r"[。！？.!?]", item.content)
        for sentence in sentences:
            sentence = sentence.strip()
            if any(kw in sentence.lower() for kw in keywords):
                rejected.append(sentence)
                if len(rejected) >= 3:
                    return rejected

    return rejected[:3]


def _extract_last_error(delta_items: list[DeltaItem]) -> str:
    """提取最后的错误"""
    for item in reversed(delta_items):
        if item.is_tool_result:
            if any(kw in item.content for kw in ["Error", "Traceback", "FAILED", "错误"]):
                # 截取前 200 字符
                error = item.content[:200]
                if len(item.content) > 200:
                    error += "..."
                return error
    return ""

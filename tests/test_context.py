"""上下文治理系统测试"""

from __future__ import annotations

import asyncio

from paicli.config import PaiCliConfig
from paicli.context import ContextManager
from paicli.context.assembler import (
    Section,
    SectionType,
    allocate_section_budgets,
    assemble_prompt,
)
from paicli.context.budget import Budget, calculate_budget
from paicli.context.compaction import (
    LLM_SUMMARY_INPUT_LIMIT,
    DeltaItem,
    compact_with_llm,
    deterministic_compact,
    extract_delta_items,
)
from paicli.context.pressure import (
    PressureTier,
    calculate_pressure,
)
from paicli.context.token_estimator import TokenEstimator, estimate_tokens
from paicli.context.tool_result import (
    compress_old_tool_results,
    offload_large_tool_results,
)
from paicli.types import Message


class SummaryLlm:
    model_name = "summary-model"
    provider_name = "fake"
    max_context_window = 200

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        self.calls += 1
        yield {
            "type": "text_delta",
            "text": "## Goal\nSummarized old context\n\n## Next Steps\nContinue.",
        }
        yield {"type": "usage", "usage": {"input_tokens": 10, "output_tokens": 5}}
        yield {"type": "message_end", "stop_reason": "end_turn"}


def test_oversized_compaction_uses_map_reduce_without_dropping_late_items():
    class ChunkLlm(SummaryLlm):
        async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
            self.calls += 1
            text = str(messages[0].content)
            marker = "late-marker" if "late-marker" in text else "early-marker"
            yield {"type": "text_delta", "text": f"## Goal\n{marker}\n\n## Next Steps\nContinue."}

    items = [
        DeltaItem(turn_id=index, role="user", content=("x" * 1900) + marker)
        for index, marker in enumerate(["early-marker"] * 11 + ["late-marker"])
    ]
    llm = ChunkLlm()
    result = asyncio.run(compact_with_llm(items, llm))

    assert len(items) * 1900 > LLM_SUMMARY_INPUT_LIMIT
    assert result.used_llm
    assert llm.calls >= 3
    assert "late-marker" in result.summary


def _small_context_config() -> PaiCliConfig:
    config = PaiCliConfig()
    config.context.min_budget_chars = 100
    config.context.max_budget_chars = 100
    config.context.output_reserve_tokens = 0
    config.context.protected_turns = 1
    return config


class TestTokenEstimator:
    """Token 估算测试"""

    def test_estimate_empty_text(self):
        """空文本估算"""
        assert estimate_tokens("") == 0

    def test_estimate_english_text(self):
        """英文文本估算"""
        # 英文大约 4 chars/token
        text = "hello world"  # 11 chars
        tokens = estimate_tokens(text)
        assert tokens >= 2  # 11/4 ≈ 2.75

    def test_estimate_chinese_text(self):
        """中文文本估算"""
        # 中文大约 1.8 chars/token
        text = "你好世界"  # 4 chars
        tokens = estimate_tokens(text)
        assert tokens >= 2  # 4/1.8 ≈ 2.2

    def test_calibration(self):
        """校准功能测试"""
        estimator = TokenEstimator()

        # 初始估算
        text = "test text with more words to make it longer"
        estimated = estimator.estimate(text)

        # 校准：实际是估算的 2 倍
        estimator.calibrate(estimated, int(estimated * 2))

        # 再次估算应该更大
        new_estimated = estimator.estimate(text)
        # 由于校准系数是 2.0，新估算应该是原来的 2 倍
        assert new_estimated >= estimated * 1.9  # 允许一些舍入误差


class TestBudget:
    """预算计算测试"""

    def test_calculate_budget_basic(self):
        """基本预算计算"""
        budget = calculate_budget(
            context_window=128_000,
            utilization_rate=0.5,
            output_reserve_tokens=4096,
        )

        assert budget.context_window == 128_000
        assert budget.prompt_tokens > 0
        assert budget.prompt_chars > 0

    def test_calculate_budget_utilization(self):
        """利用率影响预算"""
        budget_50 = calculate_budget(128_000, utilization_rate=0.5)
        budget_70 = calculate_budget(128_000, utilization_rate=0.7)

        assert budget_70.prompt_tokens > budget_50.prompt_tokens

    def test_calculate_budget_bounds(self):
        """预算上下界保护"""
        # 小 context window 应该被提升到最小值
        budget_small = calculate_budget(10_000)
        assert budget_small.prompt_chars >= 60_000

        # 大 context window 应该被限制到最大值
        budget_large = calculate_budget(2_000_000)
        assert budget_large.prompt_chars <= 800_000


class TestToolResultCompression:
    """工具结果压缩测试"""

    def test_compress_old_tool_results_keeps_recent(self):
        """保留最近 N 条工具结果"""
        messages = [
            Message(role="user", content="request 1"),
            Message(role="assistant", content="response 1"),
            Message(role="tool", content="result 1", tool_call_id="call_1"),
            Message(role="user", content="request 2"),
            Message(role="assistant", content="response 2"),
            Message(role="tool", content="result 2", tool_call_id="call_2"),
            Message(role="user", content="request 3"),
            Message(role="assistant", content="response 3"),
            Message(role="tool", content="result 3", tool_call_id="call_3"),
        ]

        compressed = compress_old_tool_results(messages, keep_recent=2)

        # 应该保留最近 2 条，压缩第 1 条
        tool_results = [m for m in compressed if m.role == "tool"]
        assert len(tool_results) == 3

        # 第一条应该被压缩
        assert "工具结果已压缩" in tool_results[0].content
        assert tool_results[1].content == "result 2"
        assert tool_results[2].content == "result 3"

    def test_compress_old_tool_results_no_compression_needed(self):
        """不需要压缩时保持不变"""
        messages = [
            Message(role="tool", content="result 1", tool_call_id="call_1"),
            Message(role="tool", content="result 2", tool_call_id="call_2"),
        ]

        compressed = compress_old_tool_results(messages, keep_recent=5)

        # 不应该有压缩
        assert all(m.content.startswith("result") for m in compressed)

    def test_offload_large_tool_results(self, tmp_path):
        """大工具结果落盘"""
        # 创建一个大的工具结果
        large_content = "x" * (150 * 1024)  # 150KB
        small_content = "small result"

        messages = [
            Message(role="tool", content=large_content, tool_call_id="call_large"),
            Message(role="tool", content=small_content, tool_call_id="call_small"),
        ]

        storage_dir = str(tmp_path / "tool_results")
        offloaded = offload_large_tool_results(
            messages,
            max_total_bytes=100 * 1024,  # 100KB
            storage_dir=storage_dir,
            session_id="test",
        )

        # 大的应该被落盘
        large_msg = offloaded[0]
        assert "大工具结果已落盘" in large_msg.content
        assert storage_dir in large_msg.content

        # 小的应该保留
        small_msg = offloaded[1]
        assert small_msg.content == small_content


class TestAssembler:
    """组装器测试"""

    def test_allocate_section_budgets(self):
        """Section 预算分配"""
        budget = Budget(
            prompt_tokens=50_000,
            output_tokens=4096,
            total_tokens=54_096,
            prompt_chars=200_000,
            context_window=128_000,
            utilization_rate=0.5,
        )

        allocations = allocate_section_budgets(budget)

        # 检查比例
        assert allocations[SectionType.PREFIX] == int(200_000 * 0.15)
        assert allocations[SectionType.MEMORY] == int(200_000 * 0.10)
        assert allocations[SectionType.SKILLS] == int(200_000 * 0.10)
        assert allocations[SectionType.RELEVANT_MEMORY] == int(200_000 * 0.10)
        assert allocations[SectionType.HISTORY] == int(200_000 * 0.45)
        assert allocations[SectionType.CURRENT_REQUEST] == int(200_000 * 0.10)

    def test_assemble_prompt_order(self):
        """组装顺序正确"""
        budget = calculate_budget(128_000)

        assembled = assemble_prompt(
            prefix="system prompt",
            memory="memory content",
            skills="skill descriptions",
            relevant_memory="relevant memories",
            history=[Message(role="user", content="hello")],
            current_request="current request",
            budget=budget,
        )

        # 检查 section 顺序
        section_types = [s.type for s in assembled.sections]
        assert section_types == [
            SectionType.PREFIX,
            SectionType.MEMORY,
            SectionType.SKILLS,
            SectionType.RELEVANT_MEMORY,
            SectionType.HISTORY,
            SectionType.CURRENT_REQUEST,
        ]


class TestContextManagerBuildTurnContext:
    def test_build_turn_context_compacts_old_history_into_actual_messages(self, tmp_path):
        config = _small_context_config()
        manager = ContextManager(
            config=config,
            llm_client=SummaryLlm(),
            cwd=str(tmp_path),
        )
        old_secret = "OLD_UNCOMPRESSED_HISTORY"
        messages = []
        for index in range(4):
            messages.append(Message(role="user", content=f"{old_secret} user {index} " * 20))
            messages.append(
                Message(
                    role="assistant",
                    content=f"{old_secret} assistant {index} " * 20,
                )
            )
        messages.extend(
            [
                Message(role="user", content="recent protected request"),
                Message(role="assistant", content="recent protected response"),
                Message(role="user", content="current request " * 80),
            ]
        )

        result = asyncio.run(manager.build_turn_context(prefix="system prompt", messages=messages))

        assert result.compacted
        assert result.pressure_tier == "tier3_summary"
        assert manager._last_compaction is not None
        assert manager._last_compaction.used_llm
        rendered_messages = "\n".join(str(message.content) for message in result.messages)
        assert "Summarized old context" in rendered_messages
        assert old_secret not in rendered_messages
        assert "recent protected request" in rendered_messages
        assert "current request" in rendered_messages

    def test_context_compression_flag_disables_history_summary_but_keeps_tool_result_compression(
        self,
        tmp_path,
    ):
        config = _small_context_config()
        config.features.context_compression = False
        llm = SummaryLlm()
        manager = ContextManager(config=config, llm_client=llm, cwd=str(tmp_path))
        messages = []
        for index in range(6):
            messages.append(Message(role="user", content=f"request {index}"))
            messages.append(Message(role="assistant", content=f"response {index}"))
            messages.append(
                Message(
                    role="tool",
                    content=f"old tool result {index}",
                    tool_call_id=f"call_{index}",
                )
            )
        messages.append(Message(role="user", content="current request " * 80))

        result = asyncio.run(manager.build_turn_context(prefix="system prompt", messages=messages))

        assert not result.compacted
        assert llm.calls == 0
        rendered_messages = "\n".join(str(message.content) for message in result.messages)
        assert "Summarized old context" not in rendered_messages
        assert "old tool result 0" not in rendered_messages
        assert "tool_call_id=call_0" in rendered_messages


class TestPressure:
    """压力感知测试"""

    def test_calculate_pressure_tier0(self):
        """Tier 0: 压力 < 60%"""
        budget = calculate_budget(128_000)

        # 创建一个小的 assembled prompt
        from paicli.context.assembler import AssembledPrompt

        assembled = AssembledPrompt()
        assembled.add_section(
            Section(
                type=SectionType.PREFIX,
                content="x" * 10_000,  # 小内容
                budget_chars=100_000,
            )
        )

        pressure = calculate_pressure(assembled, budget)

        assert pressure.tier == PressureTier.TIER0_OBSERVE
        assert pressure.pressure_ratio < 0.60

    def test_calculate_pressure_tier3(self):
        """Tier 3: 压力 >= 95%"""
        budget = calculate_budget(128_000, utilization_rate=0.1)  # 小预算

        # 创建一个大的 assembled prompt
        from paicli.context.assembler import AssembledPrompt

        assembled = AssembledPrompt()
        assembled.add_section(
            Section(
                type=SectionType.PREFIX,
                content="x" * 500_000,  # 大内容
                budget_chars=100_000,
            )
        )

        pressure = calculate_pressure(assembled, budget)

        assert pressure.tier == PressureTier.TIER3_SUMMARY
        assert pressure.pressure_ratio >= 0.95


class TestCompaction:
    """压缩测试"""

    def test_extract_delta_items(self):
        """提取待压缩项"""
        messages = [
            Message(role="user", content="msg 1"),
            Message(role="assistant", content="reply 1"),
            Message(role="tool", content="result 1"),
            Message(role="user", content="msg 2"),
            Message(role="assistant", content="reply 2"),
            Message(role="user", content="msg 3"),  # 受保护
            Message(role="assistant", content="reply 3"),  # 受保护
        ]

        delta, protected = extract_delta_items(messages, protected_turns=2)

        # 前 3 个应该被压缩，后 4 个受保护
        assert len(delta) == 3
        assert len(protected) == 4

    def test_deterministic_compact(self):
        """确定性压缩"""
        delta_items = [
            DeltaItem(turn_id=0, role="user", content="我想创建一个网站"),
            DeltaItem(turn_id=1, role="assistant", content="好的，我会帮你创建"),
            DeltaItem(
                turn_id=2,
                role="tool",
                content="read_file: /path/to/file.py",
                tool_call_id="call_1",
            ),
        ]

        result = deterministic_compact(delta_items)

        assert result.summary
        assert result.compacted_items == 3
        assert not result.used_llm

        # 摘要应该包含关键信息
        assert "网站" in result.summary or "创建" in result.summary

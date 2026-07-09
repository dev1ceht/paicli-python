"""分层组装模块

阶段二：按固定顺序组装 prompt，并为不同信息层分配预算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from paicli.context.budget import Budget
from paicli.context.token_estimator import estimate_tokens
from paicli.context.tool_result import apply_tool_result_compression
from paicli.types import Message


class SectionType(str, Enum):
    """Section 类型"""
    PREFIX = "prefix"
    MEMORY = "memory"
    SKILLS = "skills"
    RELEVANT_MEMORY = "relevant_memory"
    HISTORY = "history"
    CURRENT_REQUEST = "current_request"


# Section 预算分配比例
SECTION_BUDGET_RATIOS = {
    SectionType.PREFIX: 0.15,
    SectionType.MEMORY: 0.10,
    SectionType.SKILLS: 0.10,
    SectionType.RELEVANT_MEMORY: 0.10,
    SectionType.HISTORY: 0.45,
    SectionType.CURRENT_REQUEST: 0.10,
}

# Section 裁剪优先级（数字越小优先级越高，越先被裁剪）
SECTION_TRIM_PRIORITY = {
    SectionType.RELEVANT_MEMORY: 1,  # 最高优先级，最先裁剪
    SectionType.SKILLS: 2,
    SectionType.HISTORY: 3,
    SectionType.MEMORY: 4,
    SectionType.PREFIX: 5,  # 最低优先级，最后裁剪
    SectionType.CURRENT_REQUEST: 999,  # 不裁剪
}


@dataclass
class Section:
    """Prompt Section"""
    
    type: SectionType
    content: str
    budget_chars: int
    
    @property
    def actual_chars(self) -> int:
        """实际字符数"""
        return len(self.content)
    
    @property
    def actual_tokens(self) -> int:
        """实际 token 数"""
        return estimate_tokens(self.content)
    
    @property
    def budget_tokens(self) -> int:
        """预算 token 数"""
        return self.budget_chars // 4
    
    @property
    def is_over_budget(self) -> bool:
        """是否超预算"""
        return self.actual_chars > self.budget_chars
    
    @property
    def overflow_chars(self) -> int:
        """超出预算的字符数"""
        return max(0, self.actual_chars - self.budget_chars)
    
    def trim_to_budget(self) -> None:
        """裁剪到预算大小"""
        if self.is_over_budget:
            # 简单截断（后续可以优化为智能截断）
            self.content = self.content[:self.budget_chars]


@dataclass
class AssembledPrompt:
    """组装后的 prompt"""
    
    sections: list[Section] = field(default_factory=list)
    total_chars: int = 0
    total_tokens: int = 0
    
    def add_section(self, section: Section) -> None:
        """添加 section"""
        self.sections.append(section)
        self.total_chars += section.actual_chars
        self.total_tokens += section.actual_tokens
    
    def get_section(self, section_type: SectionType) -> Section | None:
        """获取指定类型的 section"""
        for section in self.sections:
            if section.type == section_type:
                return section
        return None
    
    def to_string(self) -> str:
        """转换为字符串"""
        parts = []
        for section in self.sections:
            if section.content.strip():
                parts.append(section.content)
        return "\n\n".join(parts)


def allocate_section_budgets(budget: Budget) -> dict[SectionType, int]:
    """分配 section 预算
    
    Args:
        budget: 总预算
        
    Returns:
        每个 section 的字符预算
    """
    allocations = {}
    for section_type, ratio in SECTION_BUDGET_RATIOS.items():
        allocations[section_type] = int(budget.prompt_chars * ratio)
    
    return allocations


def build_history_section(
    messages: list[Message],
    budget_chars: int,
    *,
    keep_recent_tool_results: int = 5,
    max_tool_result_bytes: int = 200 * 1024,
    tool_result_preview_chars: int = 200,
    tool_result_storage_dir: str = "~/.paicli/tool_results",
    session_id: str = "default",
) -> Section:
    """构建 history section
    
    内部执行工具结果裁剪：
    1. 压缩旧工具结果
    2. 大工具结果落盘
    3. 常规截断
    
    Args:
        messages: 消息列表
        budget_chars: 字符预算
        keep_recent_tool_results: 保留最近 N 条工具结果
        max_tool_result_bytes: 工具结果最大总字节数
        tool_result_preview_chars: 工具结果预览字符数
        tool_result_storage_dir: 工具结果存储目录
        session_id: 会话 ID
        
    Returns:
        history section
    """
    # 1. 应用工具结果裁剪
    messages = apply_tool_result_compression(
        messages,
        keep_recent=keep_recent_tool_results,
        max_total_bytes=max_tool_result_bytes,
        preview_chars=tool_result_preview_chars,
        storage_dir=tool_result_storage_dir,
        session_id=session_id,
    )
    
    # 2. 序列化为字符串
    content = _serialize_messages(messages)
    
    # 3. 创建 section
    section = Section(
        type=SectionType.HISTORY,
        content=content,
        budget_chars=budget_chars,
    )
    
    # 4. 如果超预算，截断
    if section.is_over_budget:
        section.trim_to_budget()
    
    return section


def _serialize_messages(messages: list[Message]) -> str:
    """序列化消息列表为字符串"""
    parts = []
    for msg in messages:
        role_label = {
            "system": "System",
            "user": "User",
            "assistant": "Assistant",
            "tool": "Tool",
        }.get(msg.role, msg.role)
        
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        parts.append(f"[{role_label}] {content}")
    
    return "\n".join(parts)


def assemble_prompt(
    *,
    prefix: str = "",
    memory: str = "",
    skills: str = "",
    relevant_memory: str = "",
    history: list[Message] | None = None,
    current_request: str = "",
    budget: Budget,
    # 工具结果裁剪参数
    keep_recent_tool_results: int = 5,
    max_tool_result_bytes: int = 200 * 1024,
    tool_result_preview_chars: int = 200,
    tool_result_storage_dir: str = "~/.paicli/tool_results",
    session_id: str = "default",
) -> AssembledPrompt:
    """组装 prompt
    
    按固定顺序组装各 section：
    1. prefix（系统指令、工作区上下文）
    2. memory（工作记忆、todo、checkpoint）
    3. skills（技能描述）
    4. relevant_memory（向量检索命中）
    5. history（对话历史 + 工具结果）
    6. current_request（当前用户输入，不裁剪）
    
    Args:
        prefix: 系统指令
        memory: 工作记忆
        skills: 技能描述
        relevant_memory: 相关记忆
        history: 对话历史
        current_request: 当前请求
        budget: 总预算
        keep_recent_tool_results: 保留最近 N 条工具结果
        max_tool_result_bytes: 工具结果最大总字节数
        tool_result_preview_chars: 工具结果预览字符数
        tool_result_storage_dir: 工具结果存储目录
        session_id: 会话 ID
        
    Returns:
        组装后的 prompt
    """
    # 分配预算
    allocations = allocate_section_budgets(budget)
    
    # 创建 sections
    assembled = AssembledPrompt()
    
    # 1. Prefix
    assembled.add_section(Section(
        type=SectionType.PREFIX,
        content=prefix,
        budget_chars=allocations[SectionType.PREFIX],
    ))
    
    # 2. Memory
    assembled.add_section(Section(
        type=SectionType.MEMORY,
        content=memory,
        budget_chars=allocations[SectionType.MEMORY],
    ))
    
    # 3. Skills
    assembled.add_section(Section(
        type=SectionType.SKILLS,
        content=skills,
        budget_chars=allocations[SectionType.SKILLS],
    ))
    
    # 4. Relevant Memory
    assembled.add_section(Section(
        type=SectionType.RELEVANT_MEMORY,
        content=relevant_memory,
        budget_chars=allocations[SectionType.RELEVANT_MEMORY],
    ))
    
    # 5. History（含工具结果裁剪）
    history_section = build_history_section(
        history or [],
        allocations[SectionType.HISTORY],
        keep_recent_tool_results=keep_recent_tool_results,
        max_tool_result_bytes=max_tool_result_bytes,
        tool_result_preview_chars=tool_result_preview_chars,
        tool_result_storage_dir=tool_result_storage_dir,
        session_id=session_id,
    )
    assembled.add_section(history_section)
    
    # 6. Current Request（不裁剪）
    assembled.add_section(Section(
        type=SectionType.CURRENT_REQUEST,
        content=current_request,
        budget_chars=allocations[SectionType.CURRENT_REQUEST],
    ))
    
    return assembled

"""压力感知裁剪模块

阶段三：使用压力分级做渐进式降级。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from paicli.context.assembler import (
    AssembledPrompt,
    Section,
    SectionType,
    SECTION_TRIM_PRIORITY,
)
from paicli.context.budget import Budget
from paicli.context.token_estimator import estimate_tokens


class PressureTier(str, Enum):
    """压力等级"""
    TIER0_OBSERVE = "tier0_observe"      # < 60%: 不处理
    TIER1_SNIP = "tier1_snip"            # 60-80%: 收紧 relevant_memory
    TIER2_PRUNE = "tier2_prune"          # 80-95%: tier1 + skills 缩到 50%
    TIER3_SUMMARY = "tier3_summary"      # >= 95%: 触发 compaction


@dataclass
class PressureResult:
    """压力计算结果"""
    
    tier: PressureTier
    pressure_ratio: float
    rendered_tokens: int
    raw_tokens: int
    budget_tokens: int
    
    @property
    def pressure_percent(self) -> float:
        """压力百分比"""
        return self.pressure_ratio * 100
    
    @property
    def needs_compaction(self) -> bool:
        """是否需要触发压缩"""
        return self.tier == PressureTier.TIER3_SUMMARY


def calculate_pressure(
    assembled: AssembledPrompt,
    budget: Budget,
) -> PressureResult:
    """计算压力
    
    pressure_ratio = max(rendered_tokens, raw_tokens) / budget_tokens
    
    Args:
        assembled: 组装后的 prompt
        budget: 预算
        
    Returns:
        压力计算结果
    """
    # 计算渲染后的 token 数
    rendered_tokens = assembled.total_tokens
    
    # 计算原始 token 数（裁剪前）
    # 这里简化处理，假设 assembled 已经包含了裁剪后的内容
    # 实际使用时需要在 assembler 中记录原始 token 数
    raw_tokens = rendered_tokens  # TODO: 从 assembler 传递原始 token 数
    
    # 预算 token 数
    budget_tokens = budget.prompt_tokens
    
    # 计算压力比
    if budget_tokens == 0:
        pressure_ratio = 1.0
    else:
        pressure_ratio = max(rendered_tokens, raw_tokens) / budget_tokens
    
    # 确定压力等级
    if pressure_ratio < 0.60:
        tier = PressureTier.TIER0_OBSERVE
    elif pressure_ratio < 0.80:
        tier = PressureTier.TIER1_SNIP
    elif pressure_ratio < 0.95:
        tier = PressureTier.TIER2_PRUNE
    else:
        tier = PressureTier.TIER3_SUMMARY
    
    return PressureResult(
        tier=tier,
        pressure_ratio=pressure_ratio,
        rendered_tokens=rendered_tokens,
        raw_tokens=raw_tokens,
        budget_tokens=budget_tokens,
    )


def apply_pressure_tier(
    assembled: AssembledPrompt,
    pressure: PressureResult,
) -> AssembledPrompt:
    """应用压力等级裁剪
    
    根据压力等级执行不同的裁剪策略：
    - tier0: 不处理
    - tier1: relevant_memory 缩到 70%
    - tier2: tier1 + skills 缩到 50%
    - tier3: 触发 compaction（返回 assembled，由调用方处理）
    
    Args:
        assembled: 组装后的 prompt
        pressure: 压力计算结果
        
    Returns:
        裁剪后的 prompt
    """
    if pressure.tier == PressureTier.TIER0_OBSERVE:
        # 不处理
        return assembled
    
    if pressure.tier == PressureTier.TIER1_SNIP:
        # relevant_memory 缩到 70%
        _trim_section(assembled, SectionType.RELEVANT_MEMORY, 0.70)
        return assembled
    
    if pressure.tier == PressureTier.TIER2_PRUNE:
        # tier1 + skills 缩到 50%
        _trim_section(assembled, SectionType.RELEVANT_MEMORY, 0.70)
        _trim_section(assembled, SectionType.SKILLS, 0.50)
        return assembled
    
    # tier3: 触发 compaction，由调用方处理
    return assembled


def _trim_section(assembled: AssembledPrompt, section_type: SectionType, ratio: float) -> None:
    """裁剪 section 到指定比例
    
    Args:
        assembled: 组装后的 prompt
        section_type: section 类型
        ratio: 目标比例（0.0-1.0）
    """
    section = assembled.get_section(section_type)
    if not section:
        return
    
    # 计算目标字符数
    target_chars = int(section.actual_chars * ratio)
    
    # 截断
    if section.actual_chars > target_chars:
        section.content = section.content[:target_chars]
        
        # 更新总字符数和 token 数
        _recalculate_totals(assembled)


def _recalculate_totals(assembled: AssembledPrompt) -> None:
    """重新计算总字符数和 token 数"""
    assembled.total_chars = sum(section.actual_chars for section in assembled.sections)
    assembled.total_tokens = sum(section.actual_tokens for section in assembled.sections)


def apply_overflow_fallback(
    assembled: AssembledPrompt,
    budget: Budget,
) -> AssembledPrompt:
    """溢出兜底裁剪
    
    当 compaction 后仍超预算时，按固定顺序继续裁剪：
    relevant_memory -> skills -> history -> memory -> prefix
    
    这个顺序对应信息恢复成本：
    - relevant_memory: 下一轮检索可补回，恢复成本最低
    - skills: 固定描述可重载，成本较低
    - history: 当前 session 局部记忆，丢失后只能靠摘要补
    - memory: 包含 todo、checkpoint，丢失会影响任务连续性
    - prefix: 包含系统规则，裁剪会影响 prompt cache 和基础行为
    
    Args:
        assembled: 组装后的 prompt
        budget: 预算
        
    Returns:
        裁剪后的 prompt
    """
    # 按裁剪优先级排序（不包括 current_request）
    trim_order = [
        SectionType.RELEVANT_MEMORY,
        SectionType.SKILLS,
        SectionType.HISTORY,
        SectionType.MEMORY,
        SectionType.PREFIX,
    ]
    
    # 循环裁剪，直到不超预算或无法继续裁剪
    max_iterations = 10  # 防止无限循环
    iteration = 0
    
    while assembled.total_tokens > budget.prompt_tokens and iteration < max_iterations:
        iteration += 1
        trimmed_any = False
        
        for section_type in trim_order:
            if assembled.total_tokens <= budget.prompt_tokens:
                break
            
            section = assembled.get_section(section_type)
            if not section or section.actual_chars <= 100:  # 最小保留 100 字符
                continue
            
            # 裁剪 20%
            new_chars = int(section.actual_chars * 0.80)
            section.content = section.content[:new_chars]
            trimmed_any = True
            
            # 重新计算 totals
            _recalculate_totals(assembled)
        
        # 如果本轮没有裁剪任何内容，退出
        if not trimmed_any:
            break
    
    return assembled


def should_trigger_compaction(
    pressure: PressureResult,
    delta_items_count: int = 0,
) -> bool:
    """判断是否应该触发压缩
    
    触发条件：
    - tier3 且 delta items >= 6
    - 或 prompt 已超预算
    
    Args:
        pressure: 压力计算结果
        delta_items_count: 增量 items 数量
        
    Returns:
        是否应该触发压缩
    """
    # tier3 且增量足够
    if pressure.tier == PressureTier.TIER3_SUMMARY and delta_items_count >= 6:
        return True
    
    # prompt 已超预算
    if pressure.rendered_tokens > pressure.budget_tokens:
        return True
    
    return False

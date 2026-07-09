"""动态预算计算模块

阶段一：从 context window 反推当前可用的 prompt 预算。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Budget:
    """Prompt 预算"""
    
    # Token 预算
    prompt_tokens: int
    output_tokens: int
    total_tokens: int
    
    # 字符预算（1 token ≈ 4 chars）
    prompt_chars: int
    
    # 原始参数
    context_window: int
    utilization_rate: float
    
    @property
    def utilization_percent(self) -> float:
        """利用率百分比"""
        return self.utilization_rate * 100


def calculate_budget(
    context_window: int,
    *,
    utilization_rate: float = 0.50,
    output_reserve_tokens: int = 4096,
    min_budget_chars: int = 60_000,
    max_budget_chars: int = 800_000,
) -> Budget:
    """计算 prompt 预算
    
    计算流程：
    1. 可用 token = context_window - output_reserve
    2. prompt_budget = 可用 token * utilization_rate
    3. char_budget = prompt_budget * 4（1 token ≈ 4 chars）
    4. 上下界保护：min(max_budget, max(min_budget, char_budget))
    
    Args:
        context_window: 模型的 context window 大小（tokens）
        utilization_rate: 利用率，默认 0.50（50%）
        output_reserve_tokens: 为输出预留的 token 数，默认 4096
        min_budget_chars: 最小字符预算，默认 60k
        max_budget_chars: 最大字符预算，默认 800k
        
    Returns:
        Budget 对象
    """
    # 1. 可用 token
    available_tokens = max(0, context_window - output_reserve_tokens)
    
    # 2. Prompt token 预算
    prompt_tokens = int(available_tokens * utilization_rate)
    
    # 3. 字符预算（1 token ≈ 4 chars）
    prompt_chars = prompt_tokens * 4
    
    # 4. 上下界保护
    prompt_chars = max(min_budget_chars, min(max_budget_chars, prompt_chars))
    
    # 反推实际使用的 token 数（基于保护后的字符预算）
    actual_prompt_tokens = prompt_chars // 4
    
    return Budget(
        prompt_tokens=actual_prompt_tokens,
        output_tokens=output_reserve_tokens,
        total_tokens=actual_prompt_tokens + output_reserve_tokens,
        prompt_chars=prompt_chars,
        context_window=context_window,
        utilization_rate=utilization_rate,
    )


def estimate_chars_from_tokens(tokens: int) -> int:
    """从 token 数估算字符数（1 token ≈ 4 chars）"""
    return tokens * 4


def estimate_tokens_from_chars(chars: int) -> int:
    """从字符数估算 token 数（1 token ≈ 4 chars）"""
    return chars // 4

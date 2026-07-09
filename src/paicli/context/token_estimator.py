"""Token 估算模块

提供启发式 token 估算和实际 usage 校准功能。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class TokenEstimator:
    """Token 估算器，支持启发式估算和实际 usage 校准"""
    
    # 校准系数（实际 tokens / 估算 tokens）
    _calibration_factor: float = 1.0
    
    # 最近几次实际 usage 记录（用于动态校准）
    _usage_history: list[tuple[int, int]] = field(default_factory=list)
    _max_history: int = 10
    
    def estimate(self, text: str) -> int:
        """启发式估算文本的 token 数
        
        根据字符类型使用不同的比率：
        - 中文: 1.8 chars/token
        - 代码: 3.2 chars/token  
        - 英文: 4.0 chars/token
        
        Args:
            text: 要估算的文本
            
        Returns:
            估算的 token 数（应用校准系数后）
        """
        if not text:
            return 0
        
        chinese_chars = 0
        code_chars = 0
        other_chars = 0
        
        code_indicators = set('{}[]();=<>+-*/&|!?:,.')
        
        for char in text:
            if '\u4e00' <= char <= '\u9fff':
                chinese_chars += 1
            elif char in code_indicators:
                code_chars += 1
            else:
                other_chars += 1
        
        # 按不同比率估算
        estimated = (
            chinese_chars / 1.8 +
            code_chars / 3.2 +
            other_chars / 4.0
        )
        
        # 应用校准系数
        calibrated = estimated * self._calibration_factor
        
        return math.ceil(calibrated)
    
    def calibrate(self, estimated_tokens: int, actual_tokens: int) -> None:
        """用实际 usage 校准估算
        
        Args:
            estimated_tokens: 之前估算的 token 数
            actual_tokens: 实际使用的 token 数
        """
        if estimated_tokens <= 0:
            return
        
        # 记录历史
        self._usage_history.append((estimated_tokens, actual_tokens))
        if len(self._usage_history) > self._max_history:
            self._usage_history.pop(0)
        
        # 计算平均校准系数
        if self._usage_history:
            total_estimated = sum(est for est, _ in self._usage_history)
            total_actual = sum(act for _, act in self._usage_history)
            
            if total_estimated > 0:
                self._calibration_factor = total_actual / total_estimated
    
    def get_calibration_factor(self) -> float:
        """获取当前校准系数"""
        return self._calibration_factor
    
    def reset_calibration(self) -> None:
        """重置校准"""
        self._calibration_factor = 1.0
        self._usage_history.clear()


# 全局单例
_global_estimator = TokenEstimator()


def estimate_tokens(text: str) -> int:
    """估算文本的 token 数（使用全局估算器）"""
    return _global_estimator.estimate(text)


def calibrate_estimation(estimated: int, actual: int) -> None:
    """用实际 usage 校准全局估算器"""
    _global_estimator.calibrate(estimated, actual)


def get_calibration_factor() -> float:
    """获取全局估算器的校准系数"""
    return _global_estimator.get_calibration_factor()

"""工具结果裁剪模块

实现两个互补的裁剪策略：
1. 压缩旧工具结果 - 保留最近 N 条完整，更早的替换为占位符
2. 大工具结果落盘 - 超过预算的保存到磁盘，保留预览
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from paicli.types import Message


# 占位符模板
COMPRESSED_PLACEHOLDER = "[工具结果已压缩，tool_call_id={tool_call_id}，需要时重新执行]"
OFFLOADED_PLACEHOLDER = "[大工具结果已落盘，路径={file_path}，预览={preview}...]"


def compress_old_tool_results(
    messages: list[Message],
    *,
    keep_recent: int = 5,
) -> list[Message]:
    """压缩旧工具结果
    
    保留最近 keep_recent 条工具结果的完整内容，更早的替换为占位符。
    
    Args:
        messages: 消息列表
        keep_recent: 保留最近 N 条完整结果，默认 5
        
    Returns:
        裁剪后的消息列表（新列表，不修改原列表）
    """
    if not messages:
        return messages
    
    # 找出所有工具结果的位置
    tool_result_indices = [
        i for i, msg in enumerate(messages) 
        if msg.role == "tool"
    ]
    
    # 如果工具结果数量不超过 keep_recent，不需要裁剪
    if len(tool_result_indices) <= keep_recent:
        return messages
    
    # 需要压缩的索引（保留最后 keep_recent 个）
    indices_to_compress = set(tool_result_indices[:-keep_recent])
    
    # 构建新消息列表
    result = []
    for i, msg in enumerate(messages):
        if i in indices_to_compress and msg.role == "tool":
            # 替换为占位符
            tool_call_id = msg.tool_call_id or "unknown"
            placeholder = COMPRESSED_PLACEHOLDER.format(tool_call_id=tool_call_id)
            result.append(Message(
                role="tool",
                content=placeholder,
                tool_call_id=msg.tool_call_id,
            ))
        else:
            result.append(msg)
    
    return result


def offload_large_tool_results(
    messages: list[Message],
    *,
    max_total_bytes: int = 200 * 1024,  # 200KB
    preview_chars: int = 200,
    storage_dir: str = "~/.paicli/tool_results",
    session_id: str = "default",
) -> list[Message]:
    """大工具结果落盘
    
    统计所有工具结果的总大小，超过预算时按大小排序，
    把最大的保存到磁盘，上下文保留预览。
    
    Args:
        messages: 消息列表
        max_total_bytes: 最大总字节数，默认 200KB
        preview_chars: 保留的预览字符数，默认 200
        storage_dir: 存储目录，默认 ~/.paicli/tool_results
        session_id: 会话 ID，用于隔离不同会话的文件
        
    Returns:
        裁剪后的消息列表（新列表，不修改原列表）
    """
    if not messages:
        return messages
    
    # 找出所有工具结果
    tool_results = [
        (i, msg) for i, msg in enumerate(messages)
        if msg.role == "tool" and not msg.content.startswith("[工具结果已压缩")
    ]
    
    if not tool_results:
        return messages
    
    # 计算总大小
    total_bytes = sum(len(msg.content.encode('utf-8')) for _, msg in tool_results)
    
    # 如果未超过预算，不需要落盘
    if total_bytes <= max_total_bytes:
        return messages
    
    # 按大小排序（从大到小）
    tool_results.sort(
        key=lambda x: len(x[1].content.encode('utf-8')),
        reverse=True
    )
    
    # 准备存储目录
    storage_path = Path(storage_dir).expanduser() / session_id
    storage_path.mkdir(parents=True, exist_ok=True)
    
    # 逐步落盘，直到总大小 <= 预算
    result = list(messages)
    current_total = total_bytes
    
    for idx, msg in tool_results:
        if current_total <= max_total_bytes:
            break
        
        # 保存到磁盘
        tool_call_id = msg.tool_call_id or f"tool_{idx}"
        file_path = storage_path / f"{tool_call_id}.txt"
        
        try:
            file_path.write_text(msg.content, encoding='utf-8')
        except OSError:
            # 写入失败，跳过这个结果
            continue
        
        # 生成预览
        preview = msg.content[:preview_chars]
        if len(msg.content) > preview_chars:
            preview = preview.rstrip() + "..."
        
        # 替换为占位符
        placeholder = OFFLOADED_PLACEHOLDER.format(
            file_path=str(file_path),
            preview=preview,
        )
        
        result[idx] = Message(
            role="tool",
            content=placeholder,
            tool_call_id=msg.tool_call_id,
        )
        
        # 更新当前总大小
        current_total -= len(msg.content.encode('utf-8'))
        current_total += len(placeholder.encode('utf-8'))
    
    return result


def apply_tool_result_compression(
    messages: list[Message],
    *,
    keep_recent: int = 5,
    max_total_bytes: int = 200 * 1024,
    preview_chars: int = 200,
    storage_dir: str = "~/.paicli/tool_results",
    session_id: str = "default",
) -> list[Message]:
    """应用工具结果裁剪（组合两个策略）
    
    先压缩旧结果，再落盘大结果。
    
    Args:
        messages: 消息列表
        keep_recent: 保留最近 N 条完整结果
        max_total_bytes: 最大总字节数
        preview_chars: 保留的预览字符数
        storage_dir: 存储目录
        session_id: 会话 ID
        
    Returns:
        裁剪后的消息列表
    """
    # 1. 压缩旧工具结果
    messages = compress_old_tool_results(messages, keep_recent=keep_recent)
    
    # 2. 大工具结果落盘
    messages = offload_large_tool_results(
        messages,
        max_total_bytes=max_total_bytes,
        preview_chars=preview_chars,
        storage_dir=storage_dir,
        session_id=session_id,
    )
    
    return messages

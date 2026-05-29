"""字幕 -> AI 视频总结。

与具体 LLM 解耦：调用方注入一个 `async (prompt: str) -> str` 的函数即可，
本模块只负责拼 Prompt 与做基本清洗。对应效果图底部的「AI 视频总结」区块。
"""

import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

_PROMPT_TMPL = (
    "你是 B站视频内容总结助手。请根据下面的视频标题和字幕，"
    "用简洁、客观的简体中文写一段总结（{limit}**一段话**，"
    "不要分点、不要使用 Markdown、不要复述“本视频”之类的套话），"
    "准确概括视频讲了什么、核心观点或结论。\n\n"
    "视频标题：{title}\n"
    "视频字幕：\n{subtitle}\n"
)


async def summarize(
    title: str,
    subtitle_text: str,
    llm_ask: Callable[[str], Awaitable[str]],
    max_chars: int = 120,
) -> Optional[str]:
    """生成视频总结文本，失败返回 None。max_chars<=0 表示不限制字数。"""
    if not subtitle_text:
        return None
    limit = f"{max_chars} 字以内，" if max_chars and max_chars > 0 else ""
    prompt = _PROMPT_TMPL.format(limit=limit, title=title or "（无标题）", subtitle=subtitle_text)
    try:
        result = await llm_ask(prompt)
    except Exception as e:  # noqa: BLE001
        logger.error("AI 总结调用失败: %s", e)
        return None
    if not result:
        return None
    # 清洗：去掉可能的多余空白与首尾引号
    text = result.strip().strip("“”\"'")
    return text or None

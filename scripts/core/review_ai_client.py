#!/usr/bin/env python3
"""审查专用模型调用封装（兼容层）。

实际请求统一走 core.llm_client 的 OpenAI 兼容通道；
每个审查 Agent 可独立配置 provider/model（与生成端模型分离）。
"""
from __future__ import annotations

from pathlib import Path

from core.llm_client import LLMError, call_llm


class ReviewAIError(RuntimeError):
    pass


def resolve_review_provider(config: dict, section: str) -> str:
    from core.llm_client import resolve_provider
    return resolve_provider(config, section)


def call_review_ai(
    config: dict,
    project_dir: Path,
    section: str,
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int,
    temperature: float,
    raw_name: str,
    fallback_retries: int = 3,
    fallback_retry_delay: float = 5.0,
    timeout: int | None = None,
) -> str:
    try:
        return call_llm(
            config,
            project_dir,
            section,
            system_prompt,
            user_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            raw_name=raw_name,
            retries=fallback_retries,
            retry_delay=fallback_retry_delay,
            timeout=timeout,
        )
    except LLMError as exc:
        raise ReviewAIError(str(exc)) from exc

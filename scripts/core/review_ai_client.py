#!/usr/bin/env python3
"""审查专用模型调用封装。

生成端继续使用 MiniMax CLI；审查端可按配置切换到 mmx、GLM、DeepSeek、
Kimi 或任意 OpenAI-compatible Chat Completions 服务。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from core.mmx_client import MmxError, call_mmx, wait_for_rate_limit, write_raw_response
from core.novel_config import (
    resolve_agent_mmx_path,
    resolve_agent_model,
    resolve_agent_qps,
)


class ReviewAIError(RuntimeError):
    pass


OPENAI_COMPATIBLE_DEFAULTS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-pro",
        "api_key_envs": ("DEEPSEEK_API_KEY",),
    },
    "kimi": {
        "base_url": "https://api.moonshot.ai/v1",
        "model": "kimi-k2.7-code",
        "api_key_envs": ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
        "omit_temperature_by_default": True,
    },
    "moonshot": {
        "base_url": "https://api.moonshot.ai/v1",
        "model": "kimi-k2.7-code",
        "api_key_envs": ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
        "omit_temperature_by_default": True,
    },
    "glm": {
        "base_url": "https://api.z.ai/api/paas/v4",
        "model": "glm-5.2",
        "api_key_envs": ("ZAI_API_KEY", "GLM_API_KEY", "ZHIPUAI_API_KEY", "BIGMODEL_API_KEY"),
    },
    "zai": {
        "base_url": "https://api.z.ai/api/paas/v4",
        "model": "glm-5.2",
        "api_key_envs": ("ZAI_API_KEY", "GLM_API_KEY", "ZHIPUAI_API_KEY", "BIGMODEL_API_KEY"),
    },
    "zhipu": {
        "base_url": "https://api.z.ai/api/paas/v4",
        "model": "glm-5.2",
        "api_key_envs": ("ZAI_API_KEY", "GLM_API_KEY", "ZHIPUAI_API_KEY", "BIGMODEL_API_KEY"),
    },
    "openai_compatible": {
        "base_url": "",
        "model": "",
        "api_key_envs": ("NOVEL_REVIEW_API_KEY",),
    },
    "openai-compatible": {
        "base_url": "",
        "model": "",
        "api_key_envs": ("NOVEL_REVIEW_API_KEY",),
    },
}


def _env_section_name(section: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in str(section or "").upper()).strip("_")


def _cfg(config: dict, name: str | None) -> dict:
    value = config.get(name, {}) if name else {}
    return value if isinstance(value, dict) else {}


def _lookup(
    config: dict,
    section: str,
    key: str,
    *,
    shared_section: str = "review_ai",
    env_aliases: tuple[str, ...] = (),
    default: Any = "",
) -> Any:
    env_key = key.upper()
    env_names = [
        f"NOVEL_{_env_section_name(section)}_{env_key}",
        *env_aliases,
        f"NOVEL_{_env_section_name(shared_section)}_{env_key}",
    ]
    for env_name in env_names:
        value = os.getenv(env_name, "").strip()
        if value:
            return value

    for cfg_name in (section, shared_section):
        cfg = _cfg(config, cfg_name)
        if key in cfg and cfg[key] not in (None, ""):
            return cfg[key]
    return default


def resolve_review_provider(config: dict, section: str) -> str:
    provider = str(
        _lookup(
            config,
            section,
            "provider",
            env_aliases=("NOVEL_REVIEW_PROVIDER",),
            default="",
        )
        or ""
    ).strip().lower()
    if not provider:
        return "mmx"
    if provider in {"minimax", "mini_max", "cli"}:
        return "mmx"
    return provider


def _resolve_http_model(config: dict, section: str, provider: str) -> str:
    model = str(
        _lookup(
            config,
            section,
            "model",
            env_aliases=("NOVEL_REVIEW_MODEL",),
            default="",
        )
        or ""
    ).strip()
    if model:
        return model
    default_model = str(OPENAI_COMPATIBLE_DEFAULTS.get(provider, {}).get("model", "") or "").strip()
    if default_model:
        return default_model
    raise ReviewAIError(f"review_ai.provider={provider} 时必须配置 review_ai.model 或 {section}.model")


def _resolve_base_url(config: dict, section: str, provider: str) -> str:
    base_url = str(
        _lookup(
            config,
            section,
            "base_url",
            env_aliases=("NOVEL_REVIEW_BASE_URL",),
            default="",
        )
        or ""
    ).strip()
    if base_url:
        return base_url
    default_base = str(OPENAI_COMPATIBLE_DEFAULTS.get(provider, {}).get("base_url", "") or "").strip()
    if default_base:
        return default_base
    raise ReviewAIError(f"review_ai.provider={provider} 时必须配置 review_ai.base_url 或 {section}.base_url")


def _resolve_api_key(config: dict, section: str, provider: str) -> str:
    direct = str(
        _lookup(
            config,
            section,
            "api_key",
            env_aliases=("NOVEL_REVIEW_API_KEY",),
            default="",
        )
        or ""
    ).strip()
    if direct:
        return direct

    api_key_env = str(
        _lookup(
            config,
            section,
            "api_key_env",
            env_aliases=("NOVEL_REVIEW_API_KEY_ENV",),
            default="",
        )
        or ""
    ).strip()
    env_names: list[str] = []
    if api_key_env:
        env_names.append(api_key_env)
    env_names.extend(OPENAI_COMPATIBLE_DEFAULTS.get(provider, {}).get("api_key_envs", ()))
    env_names.extend((
        f"NOVEL_{_env_section_name(section)}_API_KEY",
        "NOVEL_REVIEW_API_KEY",
    ))
    for env_name in env_names:
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    raise ReviewAIError(
        f"缺少 {provider} 审查 API Key，请设置 {api_key_env or '/'.join(env_names)}"
    )


def _chat_endpoint(base_url: str) -> str:
    clean = str(base_url or "").strip().rstrip("/")
    if not clean:
        return ""
    if clean.endswith("/chat/completions"):
        return clean
    return clean + "/chat/completions"


def _extract_openai_content(raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewAIError(f"HTTP审查返回非JSON: {exc}") from exc
    try:
        choice = data["choices"][0]
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if text:
                        parts.append(str(text))
                elif item:
                    parts.append(str(item))
            content = "".join(parts)
        if content and str(content).strip():
            return str(content).strip()
    except Exception as exc:
        raise ReviewAIError(f"HTTP审查响应缺少 choices[0].message.content: {exc}") from exc
    raise ReviewAIError("HTTP审查返回空内容")


def _merge_extra_body(config: dict, section: str) -> dict:
    merged: dict[str, Any] = {}
    for cfg_name in ("review_ai", section):
        cfg = _cfg(config, cfg_name)
        extra = cfg.get("extra_body")
        if isinstance(extra, dict):
            merged.update(extra)
    return merged


def _send_temperature(config: dict, section: str, provider: str) -> bool:
    value = _lookup(config, section, "send_temperature", default=None)
    if value is not None:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if OPENAI_COMPATIBLE_DEFAULTS.get(provider, {}).get("omit_temperature_by_default"):
        return False
    return True


def _call_openai_compatible(
    config: dict,
    project_dir: Path,
    section: str,
    provider: str,
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int,
    temperature: float,
    retries: int,
    retry_delay: float,
    timeout: int,
    raw_name: str,
) -> str:
    model = _resolve_http_model(config, section, provider)
    base_url = _resolve_base_url(config, section, provider)
    api_key = _resolve_api_key(config, section, provider)
    endpoint = _chat_endpoint(base_url)
    log_dir = project_dir / "logs" / "raw_responses"

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": int(max_tokens),
        "stream": False,
    }
    if _send_temperature(config, section, provider):
        payload["temperature"] = float(temperature)
    payload.update(_merge_extra_body(config, section))

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    for cfg_name in ("review_ai", section):
        extra_headers = _cfg(config, cfg_name).get("headers")
        if isinstance(extra_headers, dict):
            headers.update({str(k): str(v) for k, v in extra_headers.items()})

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    qps = resolve_agent_qps(config, section)
    rate_state_dir = project_dir / "logs" / "rate_limit"
    last_error = ""
    for attempt in range(retries + 1):
        wait_for_rate_limit(rate_state_dir, qps)
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace").strip()
            if raw:
                write_raw_response(log_dir, f"{raw_name}_{provider}", raw)
            return _extract_openai_content(raw)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")[:2000]
            last_error = f"HTTP {exc.code}: {error_body}"
        except Exception as exc:
            last_error = str(exc)
        if attempt < retries:
            time.sleep(retry_delay * (attempt + 1))
    raise ReviewAIError(last_error or f"{provider} 审查调用失败")


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
    section_cfg = _cfg(config, section)
    provider = resolve_review_provider(config, section)
    retries = int(section_cfg.get("max_retries", section_cfg.get("retries", fallback_retries)))
    retry_delay = float(section_cfg.get("retry_delay", fallback_retry_delay))
    timeout_seconds = int(section_cfg.get(
        "timeout_seconds",
        _cfg(config, "review_ai").get("timeout_seconds", timeout or 300),
    ) or timeout or 300)
    resolved_max_tokens = int(section_cfg.get("max_tokens", max_tokens) or max_tokens)
    resolved_temperature = float(section_cfg.get("temperature", temperature))

    if provider == "mmx":
        try:
            return call_mmx(
                system_prompt,
                user_prompt,
                model=resolve_agent_model(config, section, env_aliases=("NOVEL_REVIEW_MODEL",)),
                mmx_path=resolve_agent_mmx_path(
                    config,
                    project_dir,
                    section,
                    env_aliases=("NOVEL_REVIEW_MMX_PATH",),
                ),
                max_tokens=resolved_max_tokens,
                temperature=resolved_temperature,
                retries=retries,
                retry_delay=retry_delay,
                timeout=timeout_seconds,
                log_dir=project_dir / "logs" / "raw_responses",
                raw_name=raw_name,
                qps=resolve_agent_qps(config, section),
                rate_state_dir=project_dir / "logs" / "rate_limit",
            )
        except MmxError as exc:
            raise ReviewAIError(str(exc)) from exc

    if provider not in OPENAI_COMPATIBLE_DEFAULTS:
        if provider not in {"openai", "openai_compatible", "openai-compatible"}:
            raise ReviewAIError(
                f"不支持的 review_ai.provider={provider}，可选 mmx/glm/deepseek/kimi/openai_compatible"
            )
        provider = "openai_compatible"

    return _call_openai_compatible(
        config,
        project_dir,
        section,
        provider,
        system_prompt,
        user_prompt,
        max_tokens=resolved_max_tokens,
        temperature=resolved_temperature,
        retries=retries,
        retry_delay=retry_delay,
        timeout=timeout_seconds,
        raw_name=raw_name,
    )

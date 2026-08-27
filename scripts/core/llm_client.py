#!/usr/bin/env python3
"""通用 LLM 调用封装（OpenAI-compatible Chat Completions）。

每个 Agent（planner/outliner/writer/reviewer/...）可独立配置 provider/model，
通过 config 或环境变量切换 GLM / DeepSeek / Kimi 等任意 OpenAI 兼容服务。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class LLMError(RuntimeError):
    pass


SHARED_SECTION = "llm"

# 未显式配置 provider 时的默认供应商（生成端默认 DeepSeek）。
DEFAULT_PROVIDER = "deepseek"

PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "api_key_envs": ("DEEPSEEK_API_KEY",),
    },
    "kimi": {
        "base_url": "https://api.moonshot.ai/v1",
        "model": "kimi-k2.7",
        "api_key_envs": ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
        "omit_temperature_by_default": True,
    },
    "moonshot": {
        "base_url": "https://api.moonshot.ai/v1",
        "model": "kimi-k2.7",
        "api_key_envs": ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
        "omit_temperature_by_default": True,
    },
    "glm": {
        "base_url": "https://api.z.ai/api/paas/v4",
        "model": "glm-4.6",
        "api_key_envs": ("ZAI_API_KEY", "GLM_API_KEY", "ZHIPUAI_API_KEY", "BIGMODEL_API_KEY"),
    },
    "zai": {
        "base_url": "https://api.z.ai/api/paas/v4",
        "model": "glm-4.6",
        "api_key_envs": ("ZAI_API_KEY", "GLM_API_KEY", "ZHIPUAI_API_KEY", "BIGMODEL_API_KEY"),
    },
    "zhipu": {
        "base_url": "https://api.z.ai/api/paas/v4",
        "model": "glm-4.6",
        "api_key_envs": ("ZAI_API_KEY", "GLM_API_KEY", "ZHIPUAI_API_KEY", "BIGMODEL_API_KEY"),
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "api_key_envs": ("OPENAI_API_KEY",),
    },
    "openai_compatible": {
        "base_url": "",
        "model": "",
        "api_key_envs": ("NOVEL_LLM_API_KEY",),
    },
    "openai-compatible": {
        "base_url": "",
        "model": "",
        "api_key_envs": ("NOVEL_LLM_API_KEY",),
    },
}

SUPPORTED_PROVIDERS = set(PROVIDER_DEFAULTS)


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
    shared_section: str = SHARED_SECTION,
    env_aliases: tuple[str, ...] = (),
    default: Any = "",
) -> Any:
    env_names = [
        f"NOVEL_{_env_section_name(section)}_{key.upper()}",
        *env_aliases,
        f"NOVEL_{_env_section_name(shared_section)}_{key.upper()}",
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


def resolve_provider(config: dict, section: str) -> str:
    provider = str(
        _lookup(config, section, "provider", default="")
        or ""
    ).strip().lower()
    if not provider:
        return DEFAULT_PROVIDER
    return provider


def resolve_model(config: dict, section: str) -> str:
    model = str(_lookup(config, section, "model", default="") or "").strip()
    if model:
        return model
    default_model = str(PROVIDER_DEFAULTS.get(resolve_provider(config, section), {}).get("model", "") or "").strip()
    if default_model:
        return default_model
    raise LLMError(f"{section}.model 未配置且 provider 无默认模型，请设置 {section}.model 或 llm.model")


def resolve_base_url(config: dict, section: str, provider: str) -> str:
    base_url = str(_lookup(config, section, "base_url", default="") or "").strip()
    if base_url:
        return base_url
    default_base = str(PROVIDER_DEFAULTS.get(provider, {}).get("base_url", "") or "").strip()
    if default_base:
        return default_base
    raise LLMError(f"{section}.base_url 未配置且 provider={provider} 无默认地址，请设置 {section}.base_url 或 llm.base_url")


def resolve_api_key(config: dict, section: str, provider: str) -> str:
    direct = str(_lookup(config, section, "api_key", default="") or "").strip()
    if direct:
        return direct

    api_key_env = str(_lookup(config, section, "api_key_env", default="") or "").strip()
    env_names: list[str] = []
    if api_key_env:
        env_names.append(api_key_env)
    env_names.extend(PROVIDER_DEFAULTS.get(provider, {}).get("api_key_envs", ()))
    env_names.extend((
        f"NOVEL_{_env_section_name(section)}_API_KEY",
        "NOVEL_LLM_API_KEY",
    ))
    for env_name in env_names:
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    raise LLMError(
        f"缺少 {section}({provider}) 的 API Key，请设置环境变量 {api_key_env or '/'.join(env_names)}"
    )


def resolve_agent_qps(config: dict, section: str) -> float:
    for cfg_name in (section, SHARED_SECTION):
        cfg = _cfg(config, cfg_name)
        if cfg.get("api_qps") not in (None, ""):
            try:
                return float(cfg["api_qps"])
            except (TypeError, ValueError):
                pass
    try:
        return float(config.get("api_qps", 2.0))
    except (TypeError, ValueError):
        return 2.0


# ---- 多进程 QPS 限速 ----

def acquire_file_lock(lock_file: Path, stale_seconds: float = 30.0):
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            return fd
        except FileExistsError:
            try:
                age = time.time() - lock_file.stat().st_mtime
                if age > stale_seconds:
                    lock_file.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            time.sleep(0.05)


def release_file_lock(lock_file: Path, fd) -> None:
    os.close(fd)
    lock_file.unlink(missing_ok=True)


def wait_for_rate_limit(state_dir: Path, qps: float, scope: str = "llm") -> None:
    if qps <= 0:
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_file = state_dir / f"llm_rate_{scope}.lock"
    state_file = state_dir / f"llm_last_call_{scope}.json"
    fd = acquire_file_lock(lock_file)
    try:
        last_call = 0.0
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text(encoding="utf-8"))
                last_call = float(data.get("last_call", 0.0))
            except Exception as exc:
                last_call = 0.0
        interval = 1.0 / qps
        elapsed = time.time() - last_call
        remain = max(0.0, interval - elapsed)
        if remain > 0:
            time.sleep(remain)
        state_file.write_text(json.dumps({"last_call": time.time()}), encoding="utf-8")
    finally:
        release_file_lock(lock_file, fd)


def write_raw_response(log_dir: Path, name: str, raw: str) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:80]
    (log_dir / f"{stamp}_{safe_name}.raw").write_text(raw, encoding="utf-8")


def _chat_endpoint(base_url: str) -> str:
    clean = str(base_url or "").strip().rstrip("/")
    if not clean:
        return ""
    if clean.endswith("/chat/completions"):
        return clean
    return clean + "/chat/completions"


def _extract_content(raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"HTTP 响应非 JSON: {exc}") from exc
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
    except LLMError:
        raise
    except Exception as exc:
        raise LLMError(f"响应缺少 choices[0].message.content: {exc}") from exc
    raise LLMError("LLM 返回空内容")


def _send_temperature(config: dict, section: str, provider: str) -> bool:
    value = _lookup(config, section, "send_temperature", default=None)
    if value is not None:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if PROVIDER_DEFAULTS.get(provider, {}).get("omit_temperature_by_default"):
        return False
    return True


def _extra_body(config: dict, section: str) -> dict:
    merged: dict[str, Any] = {}
    for cfg_name in (SHARED_SECTION, section):
        extra = _cfg(config, cfg_name).get("extra_body")
        if isinstance(extra, dict):
            merged.update(extra)
    return merged


def _headers(config: dict, section: str, api_key: str) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    for cfg_name in (SHARED_SECTION, section):
        extra_headers = _cfg(config, cfg_name).get("headers")
        if isinstance(extra_headers, dict):
            headers.update({str(k): str(v) for k, v in extra_headers.items()})
    return headers


def call_llm(
    config: dict,
    project_dir: Path,
    section: str,
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int = 8192,
    temperature: float = 0.5,
    raw_name: str | None = None,
    retries: int | None = None,
    retry_delay: float | None = None,
    timeout: int | None = None,
) -> str:
    """按 section 配置调用 OpenAI 兼容 Chat Completions。

    provider/model/base_url/api_key 均支持 环境变量 > section 配置 > llm 共享节。
    返回纯文本内容；失败抛出 LLMError。
    """
    section_cfg = _cfg(config, section)
    provider = resolve_provider(config, section)
    if provider in {"mmx", "minimax", "mini_max"}:
        raise LLMError(
            f"{section}.provider=mmx 已废弃：请改用 openai 兼容 provider "
            "(glm/deepseek/kimi/openai_compatible 等)"
        )
    if provider not in SUPPORTED_PROVIDERS:
        raise LLMError(f"不支持的 {section}.provider={provider}，可选 {'/'.join(sorted(SUPPORTED_PROVIDERS))}")

    model = resolve_model(config, section)
    base_url = resolve_base_url(config, section, provider)
    api_key = resolve_api_key(config, section, provider)
    endpoint = _chat_endpoint(base_url)

    resolved_max_tokens = int(section_cfg.get("max_tokens", max_tokens) or max_tokens)
    resolved_temperature = float(section_cfg.get("temperature", temperature))
    resolved_retries = int(retries if retries is not None else section_cfg.get("max_retries", section_cfg.get("retries", 3)))
    resolved_retry_delay = float(retry_delay if retry_delay is not None else section_cfg.get("retry_delay", 5.0))
    resolved_timeout = int(timeout or section_cfg.get("timeout_seconds", 300))

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": resolved_max_tokens,
        "stream": False,
    }
    if _send_temperature(config, section, provider):
        payload["temperature"] = resolved_temperature
    payload.update(_extra_body(config, section))

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    log_dir = project_dir / "logs" / "raw_responses"
    name = raw_name or section
    qps = resolve_agent_qps(config, section)
    rate_state_dir = project_dir / "logs" / "rate_limit"

    last_error = ""
    for attempt in range(resolved_retries + 1):
        wait_for_rate_limit(rate_state_dir, qps, scope=provider)
        request = urllib.request.Request(endpoint, data=body, headers=_headers(config, section, api_key), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=resolved_timeout) as response:
                raw = response.read().decode("utf-8", errors="replace").strip()
            if raw:
                write_raw_response(log_dir, f"{name}_{provider}", raw)
            return _extract_content(raw)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")[:2000]
            last_error = f"HTTP {exc.code}: {error_body}"
        except Exception as exc:
            last_error = str(exc)
        if attempt < resolved_retries:
            time.sleep(resolved_retry_delay * (attempt + 1))
    raise LLMError(last_error or f"{section}({provider}/{model}) 调用失败")
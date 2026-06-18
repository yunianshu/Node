# -*- coding: utf-8 -*-
"""Kimi API 适配器：当 MiniMax API 不可用时，直接用 Kimi (Moonshot) API 生成正文。

通过 anthropic SDK + Kimi base_url 调用，输出长度不受 Claude CLI -p 模式限制。
认证信息来自 Claude CLI 的 settings.json（ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL）。
"""
import json
import subprocess
import time
from pathlib import Path


class ClaudeError(Exception):
    pass


# 从 Claude CLI settings.json 读取认证信息
def _load_kimi_credentials():
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        return None, None
    try:
        cfg = json.loads(settings_path.read_text(encoding="utf-8"))
        env = cfg.get("env", {})
        token = env.get("ANTHROPIC_AUTH_TOKEN", "")
        base_url = env.get("ANTHROPIC_BASE_URL", "")
        model = cfg.get("model", "opus[1m]")
        return token, base_url, model
    except Exception:
        return None, None, None


_KIMI_TOKEN, _KIMI_BASE_URL, _KIMI_MODEL = _load_kimi_credentials()
_kimi_client = None


def _get_kimi_client():
    global _kimi_client
    if _kimi_client is not None:
        return _kimi_client
    if not _KIMI_TOKEN or not _KIMI_BASE_URL:
        return None
    try:
        import anthropic
        _kimi_client = anthropic.Anthropic(
            api_key=_KIMI_TOKEN,
            base_url=_KIMI_BASE_URL,
        )
        return _kimi_client
    except Exception:
        return None


def call_claude(
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    retries: int = 2,
    retry_delay: float = 3.0,
    timeout: int = 180,
    log_dir: Path | None = None,
    raw_name: str = "kimi_call",
    **_unused,
) -> str:
    """调用 Kimi API 生成文本。接口与 call_mmx 兼容。

    优先用 anthropic SDK 直连 Kimi（输出长度充足），
    回退到 Claude CLI（输出可能较短）。
    """
    # 方式1：anthropic SDK 直连 Kimi（首选，输出长）
    client = _get_kimi_client()
    if client is not None:
        last_error = ""
        for attempt in range(retries + 1):
            try:
                resp = client.messages.create(
                    model=_KIMI_MODEL,
                    max_tokens=max_tokens,
                    system=system_prompt[:5000] if system_prompt else "你是专业作家。",
                    messages=[{"role": "user", "content": user_prompt}],
                )
                text = ""
                for block in resp.content:
                    if hasattr(block, "text"):
                        text += block.text
                if text and text.strip():
                    # 去除 Kimi 可能添加的 markdown 代码块包裹
                    clean = text.strip()
                    if clean.startswith("```"):
                        clines = clean.split("\n")
                        while clines and clines[0].strip().startswith("```"):
                            clines.pop(0)
                        while clines and clines[-1].strip() == "```":
                            clines.pop()
                        clean = "\n".join(clines).strip()
                    if log_dir:
                        (log_dir / f"{raw_name}.raw").write_text(clean, encoding="utf-8")
                    return clean
                last_error = "Kimi 返回空内容"
            except Exception as exc:
                last_error = f"Kimi异常: {exc}"
            if attempt < retries:
                time.sleep(retry_delay * (attempt + 1))

    # 方式2：回退到 Claude CLI（输出可能短）
    full_prompt = f"{system_prompt}\n\n{user_prompt}" if system_prompt else user_prompt
    last_error = ""
    for attempt in range(retries + 1):
        try:
            result = subprocess.run(
                [
                    "claude", "-p", full_prompt,
                    "--output-format", "text",
                    "--dangerously-skip-permissions",
                    "--no-session-persistence",
                ],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=timeout,
            )
            if result.returncode == 0 and result.stdout.strip():
                content = result.stdout.strip()
                if content.startswith("```"):
                    lines = content.split("\n")
                    while lines and lines[0].strip().startswith("```"):
                        lines.pop(0)
                    while lines and lines[-1].strip() == "```":
                        lines.pop()
                    content = "\n".join(lines).strip()
                if log_dir:
                    (log_dir / f"{raw_name}.raw").write_text(content, encoding="utf-8")
                return content
            last_error = result.stderr.strip() or f"rc={result.returncode}"
        except subprocess.TimeoutExpired:
            last_error = f"超时({timeout}s)"
        except Exception as exc:
            last_error = f"异常: {exc}"
        if attempt < retries:
            time.sleep(retry_delay * (attempt + 1))

    raise ClaudeError(last_error or "Kimi 和 Claude CLI 均不可用")

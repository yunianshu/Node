#!/usr/bin/env python3
"""MiniMax CLI 调用封装。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path


class MmxError(RuntimeError):
    pass


def extract_content(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data.get("content", raw)
    except json.JSONDecodeError:
        pass
    if "Response:" in raw:
        json_part = raw.split("Response:")[-1].strip()
        try:
            data = json.loads(json_part)
            if isinstance(data, dict):
                return data.get("content", json_part)
        except json.JSONDecodeError:
            return json_part
    return raw


def write_raw_response(log_dir: Path, name: str, raw: str) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:80]
    (log_dir / f"{stamp}_{safe_name}.raw").write_text(raw, encoding="utf-8")


def compute_wait_seconds(last_call_time: float, now: float, qps: float) -> float:
    if qps <= 0:
        return 0.0
    interval = 1.0 / qps
    elapsed = now - last_call_time
    return max(0.0, interval - elapsed)


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


def wait_for_rate_limit(state_dir: Path, qps: float) -> None:
    if qps <= 0:
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_file = state_dir / "mmx_rate.lock"
    state_file = state_dir / "mmx_last_call.json"
    fd = acquire_file_lock(lock_file)
    try:
        last_call = 0.0
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text(encoding="utf-8"))
                last_call = float(data.get("last_call", 0.0))
            except Exception:
                last_call = 0.0
        wait = compute_wait_seconds(last_call, time.time(), qps)
        if wait > 0:
            time.sleep(wait)
        state_file.write_text(json.dumps({"last_call": time.time()}), encoding="utf-8")
    finally:
        release_file_lock(lock_file, fd)


def _mmx_base_cmd(mmx_path: str) -> list[str]:
    text = str(mmx_path or "").strip()
    if not text:
        raise MmxError("mmx_path 为空，请设置 config.json 的 mmx_path 或 NOVEL_MMX_PATH")
    suffix = Path(text).suffix.lower()
    if suffix in {".mjs", ".js", ".cjs"} or Path(text).exists():
        return ["node", text]
    found = shutil.which(text)
    if found:
        return [found]
    return [text]


def call_mmx(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str,
    mmx_path: str,
    max_tokens: int,
    temperature: float,
    retries: int = 3,
    retry_delay: float = 5.0,
    log_dir: Path | None = None,
    raw_name: str = "mmx",
    timeout: int | None = None,
    qps: float = 15.0,
    rate_state_dir: Path | None = None,
) -> str:
    # Windows 命令行长度限制约 32k，prompt 过长时改用 --messages-file
    # 阈值保守一些，prompt 超过 16k 字符就改用文件
    payload_size = len(system_prompt) + len(user_prompt)
    use_file = payload_size > 16384

    if use_file:
        # 写到临时文件，用 messages-file 方式传入
        import tempfile

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        tmp_dir = Path(log_dir) if log_dir else Path(tempfile.gettempdir())
        tmp_dir.mkdir(parents=True, exist_ok=True)
        import time as _t
        tmp_path = tmp_dir / (
            f"mmx_msgs_{_t.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}_"
            f"{threading.get_ident()}_{uuid.uuid4().hex}.json"
        )
        try:
            tmp_path.write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
            cmd = [
                *_mmx_base_cmd(mmx_path), "text", "chat",
                "--model", model,
                "--messages-file", str(tmp_path),
                "--max-tokens", str(max_tokens),
                "--temperature", str(temperature),
                "--stream=false",
                "--quiet",
            ]
        except Exception:
            # 出错就回退到命令行方式
            use_file = False
            tmp_path = None
    if not use_file:
        tmp_path = None
        cmd = [
            *_mmx_base_cmd(mmx_path), "text", "chat",
            "--model", model,
            "--system", system_prompt,
            "--message", user_prompt,
            "--max-tokens", str(max_tokens),
            "--temperature", str(temperature),
            "--stream=false",
            "--quiet",
        ]

    last_error = ""
    for attempt in range(retries + 1):
        if rate_state_dir is not None:
            wait_for_rate_limit(rate_state_dir, qps)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout or 300,
            )
        except subprocess.TimeoutExpired as exc:
            last_error = f"调用超时: {exc}"
        except Exception as exc:
            last_error = f"调用异常: {exc}"
        else:
            raw = result.stdout.strip()
            if raw and log_dir:
                write_raw_response(log_dir, raw_name, raw)
            if result.returncode == 0:
                if tmp_path is not None:
                    try:
                        tmp_path.unlink()
                    except Exception:
                        pass
                return extract_content(raw)
            last_error = result.stderr.strip() or raw or f"returncode={result.returncode}"

        if attempt < retries:
            time.sleep(retry_delay * (attempt + 1))

    if tmp_path is not None:
        try:
            tmp_path.unlink()
        except Exception:
            pass
    raise MmxError(last_error or "MiniMax 调用失败")

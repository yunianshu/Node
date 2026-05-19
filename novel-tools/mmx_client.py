#!/usr/bin/env python3
"""MiniMax CLI 调用封装。"""
from __future__ import annotations

import json
import os
import subprocess
import time
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
    qps: float = 0.0,
    rate_state_dir: Path | None = None,
) -> str:
    cmd = [
        "node", mmx_path, "text", "chat",
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
                timeout=timeout,
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
                return extract_content(raw)
            last_error = result.stderr.strip() or raw or f"returncode={result.returncode}"

        if attempt < retries:
            time.sleep(retry_delay * (attempt + 1))

    raise MmxError(last_error or "MiniMax 调用失败")

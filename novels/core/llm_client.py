#!/usr/bin/env python3
"""
LLM 客户端 - 统一的 MiniMax API 调用入口
核心能力：令牌桶限流 + 智能重试 + JSON 容错解析
所有 Agent 共享同一个单例实例
"""
import json
import re
import subprocess
import time
from threading import Lock
from typing import Optional

from novels.core.logger import get_logger


class TokenBucket:
    """线程安全的令牌桶限流器"""

    def __init__(self, qps: float = 2.0, max_burst: int = 5):
        self.qps = qps
        self.max_burst = max_burst
        self._tokens = float(max_burst)
        self._last_time = time.time()
        self._lock = Lock()

    def acquire(self, timeout: float = 120.0) -> bool:
        """获取一个令牌，失败则等待。返回是否成功"""
        deadline = time.time() + timeout
        while True:
            with self._lock:
                now = time.time()
                elapsed = now - self._last_time
                self._tokens = min(self.max_burst, self._tokens + elapsed * self.qps)
                self._last_time = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
            if time.time() >= deadline:
                return False
            time.sleep(0.05)  # 50ms 轮询

    def adjust_rate(self, new_qps: float):
        """动态调整 QPS"""
        with self._lock:
            self.qps = new_qps


class LLMClient:
    """MiniMax LLM 客户端（全局单例）"""

    _instance: Optional["LLMClient"] = None
    _lock = Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        mmx_path: str,
        model: str = "MiniMax-M2.7-highspeed",
        qps: float = 2.0,
        log_dir: Optional[str] = None,
    ):
        if self._initialized:
            return

        self.mmx_path = mmx_path
        self.model = model
        self.bucket = TokenBucket(qps=qps, max_burst=int(qps * 3))
        self.logger = get_logger("LLMClient", log_dir or ".")
        self._initialized = True

    # ------------------------------------------------------------------
    # 核心调用
    # ------------------------------------------------------------------
    def call(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 8192,
        temperature: float = 0.7,
        max_retries: int = 3,
        retry_delay: float = 5.0,
    ) -> str:
        """
        调用 LLM 生成文本。
        自动处理：限流等待 -> 调用 -> 重试(指数退避) -> 响应提取
        """
        for attempt in range(max_retries):
            # 1. 限流等待
            if not self.bucket.acquire(timeout=180):
                self.logger.error("限流等待超时(180s)，跳过本次调用")
                return ""

            # 2. 执行调用
            result = self._subprocess_call(system_prompt, user_prompt, max_tokens, temperature)

            # 3. 成功
            if result["ok"]:
                return result["text"]

            err = result["error"]

            # 4. 配额超限 (2062) → 指数退避
            if "2062" in err or "Rate limit" in err or "quota exceeded" in err:
                delay = retry_delay * (2 ** attempt)
                self.logger.warning(f"API 限流(2062)，第{attempt + 1}/{max_retries}次重试，等待{delay}s...")
                time.sleep(delay)
                continue

            # 5. 其他错误 → 普通重试
            if attempt < max_retries - 1:
                self.logger.warning(f"调用失败: {err}，{attempt + 1}/{max_retries}次重试...")
                time.sleep(retry_delay)
                continue

            # 6. 重试耗尽
            self.logger.error(f"调用失败，已达最大重试次数: {err}")
            return ""

        return ""

    def call_json(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 4096,
        temperature: float = 0.3,
        max_retries: int = 3,
    ) -> dict:
        """
        调用 LLM 并安全解析 JSON 响应。
        自动处理：控制字符清洗、尾部逗号修复、反斜杠转义、代码块提取
        """
        for attempt in range(max_retries):
            text = self.call(system_prompt, user_prompt, max_tokens, temperature, max_retries=1)
            if not text:
                if attempt < max_retries - 1:
                    time.sleep(1)
                continue

            cleaned = self._sanitize_json(text)
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError as e:
                # 告知 LLM 上次解析失败原因，请求重试
                hint = f"\n\n[系统提示] 上次返回的内容 JSON 解析失败: {str(e)}。请严格输出合法 JSON，字符串内不要包含未转义的双引号、换行符或控制字符。"
                user_prompt = user_prompt + hint
                self.logger.warning(f"JSON 解析失败(第{attempt + 1}次): {e}")
                time.sleep(1)

        self.logger.error(f"JSON 解析失败，已达最大重试次数。原始文本前200字符: {text[:200] if text else '空'}")
        return {}

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _subprocess_call(
        self, system: str, user: str, max_tokens: int, temperature: float
    ) -> dict:
        """底层 subprocess 调用 mmx CLI"""
        cmd = [
            "node", self.mmx_path, "text", "chat",
            "--model", self.model,
            "--system", system,
            "--message", user,
            "--max-tokens", str(max_tokens),
            "--temperature", str(temperature),
            "--stream=false",
            "--quiet",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
            if result.returncode != 0:
                err = result.stderr.strip() if result.stderr else f"rc={result.returncode}"
                return {"ok": False, "error": err, "text": ""}
            text = self._extract_content(result.stdout.strip())
            return {"ok": True, "error": "", "text": text}
        except Exception as e:
            return {"ok": False, "error": str(e), "text": ""}

    def _extract_content(self, raw: str) -> str:
        """从 mmx CLI 输出中提取 content"""
        if not raw:
            return ""

        # 1. 尝试直接解析 JSON
        try:
            data = json.loads(raw)
            return data.get("content", raw)
        except json.JSONDecodeError:
            pass

        # 2. Response: 前缀
        if "Response:" in raw:
            part = raw.split("Response:")[-1].strip()
            try:
                return json.loads(part).get("content", part)
            except json.JSONDecodeError:
                return part

        # 3. ```json 代码块
        if "```json" in raw:
            block = raw.split("```json")[1].split("```")[0].strip()
            try:
                return json.loads(block).get("content", block)
            except json.JSONDecodeError:
                return block

        # 4. ``` 代码块
        if "```" in raw:
            block = raw.split("```")[1].strip()
            return block

        return raw

    def _sanitize_json(self, text: str) -> str:
        """
        清洗文本以使其成为合法 JSON。
        处理：控制字符、尾部逗号、未转义反斜杠、多行字符串
        """
        if not text:
            return "{}"

        # 0. 去除 BOM
        text = text.lstrip("\ufeff")

        # 1. 去除控制字符（保留 \n \r \t）
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)

        # 2. 去除零宽字符
        text = re.sub(r"[\u200b-\u200f\ufeff]", "", text)

        # 3. 修复对象/数组尾部逗号
        text = re.sub(r",(\s*[}\]])", r"\1", text)

        # 4. 修复字符串内的未转义反斜杠（仅影响 JSON 解析的）
        # 先匹配 JSON 字符串，然后处理内部的 \
        def _fix_escapes(match: re.Match) -> str:
            s = match.group(0)
            # 保留合法转义序列，其他双写
            s = re.sub(r'\\([^"\\/bfnrtu])', r'\\\\\1', s)
            return s

        text = re.sub(r'"(?:[^"\\]|\\.)*"', _fix_escapes, text)

        # 5. 去除字符串值内的多余换行（部分 LLM 会在字符串中间换行）
        # 这步比较激进，只在解析失败时作为 fallback 使用
        # 这里仅去除 JSON 结构外的空白
        text = text.strip()

        return text

    def adjust_qps(self, new_qps: float):
        """动态调整限流速率"""
        self.bucket.adjust_rate(new_qps)
        self.logger.info(f"限流 QPS 调整为 {new_qps}")

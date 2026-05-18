#!/usr/bin/env python3
"""
通知模块 - 企业微信 Webhook 推送
支持消息限流（避免频繁推送）
"""
import json
import time
import urllib.request
import urllib.error
from threading import Lock


class WeChatNotifier:
    """企业微信 Webhook 通知器"""

    def __init__(self, webhook_url: str = "", min_interval: float = 10.0):
        self.webhook_url = webhook_url
        self.min_interval = min_interval
        self._last_push = 0.0
        self._lock = Lock()

    def push(self, msg: str) -> bool:
        """推送消息，带最小间隔限制"""
        if not self.webhook_url:
            return False

        with self._lock:
            now = time.time()
            if now - self._last_push < self.min_interval:
                return False
            self._last_push = now

        try:
            data = json.dumps({"msgtype": "text", "text": {"content": msg}}).encode("utf-8")
            req = urllib.request.Request(
                self.webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            return True
        except Exception:
            return False

    def push_progress(self, title: str, draft: int, reviewed: int, total: int, total_words: int = 0):
        """推送进度消息"""
        pct = draft / total * 100 if total > 0 else 0
        msg = f"[{title}] 进度: 初稿{draft}/{total} ({pct:.1f}%), 审查{reviewed}/{total}"
        if total_words > 0:
            msg += f", 总字数{total_words:,}"
        return self.push(msg)

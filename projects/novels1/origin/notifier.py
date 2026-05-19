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
            data = json.dumps({"msgtype": "text", "text": {"content": msg}}, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                self.webhook_url,
                data=data,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            return True
        except Exception:
            return False

    def format_progress(self, title: str, outline: int = 0, draft: int = 0,
                        review: int = 0, final: int = 0, total: int = 2000,
                        total_words: int = 0, score: float = 0.0, agents: int = 0) -> str:
        """格式化进度消息为统一模板"""
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            f"【{title}】生成进度 ({now})",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"  📋 大纲:     {outline:04d}/{total:04d} 章  {'✅' if outline >= total else '...'}",
            f"  ✍  初稿:     {draft:04d}/{total:04d} 章  {'✅' if draft >= total else '🔄'}",
            f"  📝 字数:     {total_words:,}",
            f"  🔍 审查:     {review:04d}/{total:04d} 章",
            f"  📤 终稿:     {final:04d}/{total:04d} 章",
        ]
        if score > 0:
            lines.append(f"  ⭐ 平均评分: {score:.2f}")
        else:
            lines.append("  ⭐ 平均评分: --/--")
        if agents > 0:
            lines.append(f"  🤖 运行Agent: {agents} 个并行")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
        # 预计剩余时间
        if draft < total and agents > 0:
            mins = (total - draft) * 55 / 60 / agents
            lines.append(f"  预计剩余: ~{mins:.0f} 分钟 ({mins/60:.1f} 小时)")
        return "\n".join(lines)

    def push_progress(self, title: str, draft: int, reviewed: int, total: int, total_words: int = 0):
        """推送进度消息（兼容旧接口）"""
        msg = self.format_progress(title, draft=draft, review=reviewed, total=total, total_words=total_words)
        return self.push(msg)

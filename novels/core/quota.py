#!/usr/bin/env python3
"""
配额管理 - 查询 MiniMax API 配额状态
支持预检查（在批量任务前判断配额是否充足）
"""
import json
import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass
class QuotaInfo:
    name: str
    used: int
    limit: int

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    @property
    def is_exhausted(self) -> bool:
        return self.remaining <= 0


class QuotaMonitor:
    """MiniMax API 配额监控器"""

    def __init__(self, mmx_path: str):
        self.mmx_path = mmx_path
        self._cache: dict[str, QuotaInfo] = {}
        self._text_quota_name = "MiniMax-M*"

    def check_all(self, force_refresh: bool = False) -> dict[str, QuotaInfo]:
        """查询所有配额"""
        if self._cache and not force_refresh:
            return self._cache

        result = subprocess.run(
            ["node", self.mmx_path, "quota", "show", "--quiet", "--output", "json"],
            capture_output=True, text=True, encoding="utf-8"
        )

        quotas: dict[str, QuotaInfo] = {}
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                items = data if isinstance(data, list) else data.get("model_remains", [])
                for item in items:
                    name = item.get("model_name", "unknown")
                    used = item.get("current_interval_usage_count", 0)
                    limit = item.get("current_interval_total_count", 0)
                    quotas[name] = QuotaInfo(name=name, used=used, limit=limit)
            except Exception:
                pass

        # 保底默认值
        if not quotas:
            quotas[self._text_quota_name] = QuotaInfo(name=self._text_quota_name, used=0, limit=4500)

        self._cache = quotas
        return quotas

    def text_remaining(self, force_refresh: bool = False) -> int:
        """获取文本生成配额剩余量"""
        quotas = self.check_all(force_refresh)
        for name, info in quotas.items():
            if "MiniMax-M" in name or "M*" in name:
                return info.remaining
        return quotas.get(self._text_quota_name, QuotaInfo("", 0, 4500)).remaining

    def is_sufficient(self, calls_needed: int) -> bool:
        """检查剩余配额是否足够完成指定调用次数"""
        return self.text_remaining(force_refresh=True) >= calls_needed

    def format_report(self) -> str:
        """格式化配额报告"""
        quotas = self.check_all()
        lines = ["配额状态:"]
        for name, info in quotas.items():
            lines.append(f"  {name}: {info.used}/{info.limit}, 剩余 {info.remaining}")
        return "\n".join(lines)

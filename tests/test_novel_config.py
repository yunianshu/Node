"""novel_config 模块单元测试。"""
import json

from pathlib import Path

import pytest

from core.novel_config import (
    DEFAULT_CONFIG,
    deep_merge,
    get_book_title,
    get_webhook_url,
    load_config,
    validate_model_separation,
)


class TestDeepMerge:
    def test_simple_override(self):
        base = {"a": 1, "b": 2}
        override = {"b": 3}
        assert deep_merge(base, override) == {"a": 1, "b": 3}

    def test_nested_merge(self):
        base = {"a": {"x": 1, "y": 2}}
        override = {"a": {"y": 3}}
        assert deep_merge(base, override) == {"a": {"x": 1, "y": 3}}

    def test_base_unchanged(self):
        base = {"a": 1}
        deep_merge(base, {"a": 2})
        assert base == {"a": 1}


class TestLoadConfig:
    def test_missing_file_returns_default(self, tmp_path: Path):
        config = load_config(tmp_path)
        assert config["total_chapters"] == DEFAULT_CONFIG["total_chapters"]

    def test_override_values(self, tmp_path: Path):
        (tmp_path / "config.json").write_text(
            json.dumps({"total_chapters": 100}),
            encoding="utf-8",
        )
        config = load_config(tmp_path)
        assert config["total_chapters"] == 100
        assert config["writer"]["max_tokens"] == DEFAULT_CONFIG["writer"]["max_tokens"]

    def test_invalid_json_returns_default(self, tmp_path: Path):
        (tmp_path / "config.json").write_text("not json", encoding="utf-8")
        config = load_config(tmp_path)
        assert config["total_chapters"] == DEFAULT_CONFIG["total_chapters"]

    def test_no_mmx_fields_in_default(self):
        assert "mmx_path" not in DEFAULT_CONFIG
        for section in ("planner", "outliner", "writer", "polisher",
                        "reviewer", "outline_reviewer"):
            assert "mmx_path" not in DEFAULT_CONFIG[section]

    def test_outline_book_reviewer_section_removed(self):
        assert "outline_book_reviewer" not in DEFAULT_CONFIG


class TestModelSeparation:
    def test_defaults_are_separated(self):
        """默认配置下生成端与审查端模型必须不同。"""
        validate_model_separation(DEFAULT_CONFIG)

    def test_same_model_rejected(self):
        config = {
            "llm": {},
            "planner": {"provider": "deepseek", "model": "same-model"},
            "outliner": {"provider": "deepseek", "model": "same-model"},
            "writer": {"provider": "deepseek", "model": "same-model"},
            "polisher": {"provider": "deepseek", "model": "same-model"},
            "reviewer": {"provider": "deepseek", "model": "same-model"},
            "outline_reviewer": {"provider": "glm", "model": "other-model"},
        }
        with pytest.raises(ValueError, match="相同模型"):
            validate_model_separation(config)

    def test_shared_llm_section_applies(self, tmp_path: Path):
        config = load_config(tmp_path)
        assert config["writer"]["provider"] == "deepseek"
        assert config["reviewer"]["provider"] == "glm"


class TestGetBookTitle:
    def test_with_world_json(self, tmp_path: Path):
        (tmp_path / "world.json").write_text(
            json.dumps({"title": "测试小说"}), encoding="utf-8"
        )
        assert get_book_title(tmp_path) == "测试小说"

    def test_missing_world_json(self, tmp_path: Path):
        assert get_book_title(tmp_path) == "未命名小说"

    def test_custom_default(self, tmp_path: Path):
        assert get_book_title(tmp_path, default="自定义") == "自定义"

    def test_invalid_json(self, tmp_path: Path):
        (tmp_path / "world.json").write_text("bad", encoding="utf-8")
        assert get_book_title(tmp_path) == "未命名小说"

    def test_accepts_str_path(self, tmp_path: Path):
        (tmp_path / "world.json").write_text(
            json.dumps({"title": "字符串路径"}), encoding="utf-8"
        )
        assert get_book_title(str(tmp_path)) == "字符串路径"


class TestGetWebhookUrl:
    def test_from_config(self):
        assert get_webhook_url({"webhook_url": "https://example.com"}) == "https://example.com"

    def test_empty(self):
        assert get_webhook_url({}) == ""

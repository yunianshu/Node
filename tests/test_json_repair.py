"""json_repair 模块单元测试。"""
import json

from core.json_repair import (
    fix_inner_quotes,
    fix_truncated_json,
    parse_score,
    repair_latin1_gbk_mojibake,
    strip_json_markdown,
)


class TestFixInnerQuotes:
    def test_valid_json_unchanged(self):
        text = '{"key": "value"}'
        assert fix_inner_quotes(text) == text

    def test_inner_double_quotes_replaced(self):
        text = '{"desc": "他说"你好"然后走了"}'
        result = fix_inner_quotes(text)
        assert result.count("'") >= 2
        assert json.loads(result)["desc"] == "他说'你好'然后走了"

    def test_escaped_quotes_preserved(self):
        text = '{"key": "say \\"hi\\""}'
        assert fix_inner_quotes(text) == text

    def test_empty_string(self):
        assert fix_inner_quotes("") == ""

    def test_array_with_strings(self):
        text = '{"items": ["a"b", "c"d"]}'
        result = fix_inner_quotes(text)
        parsed = json.loads(result)
        assert parsed["items"][0] == "a'b"


class TestFixTruncatedJson:
    def test_complete_json_unchanged(self):
        text = '{"a": 1, "b": [2, 3]}'
        assert fix_truncated_json(text) == text

    def test_missing_closing_brace(self):
        text = '{"a": 1, "b": {"c": 2'
        result = fix_truncated_json(text)
        assert result.endswith("}")

    def test_extra_closing_braces_truncated(self):
        text = '{"a": 1}}}'
        result = fix_truncated_json(text)
        assert json.loads(result) == {"a": 1}

    def test_complete_nested_preserved(self):
        text = '{"a": {"b": 2}, "c": 3}'
        assert fix_truncated_json(text) == text


class TestStripJsonMarkdown:
    def test_plain_json_unchanged(self):
        assert strip_json_markdown('{"a": 1}') == '{"a": 1}'

    def test_json_code_block(self):
        text = '```json\n{"a": 1}\n```'
        assert strip_json_markdown(text) == '{"a": 1}'

    def test_generic_code_block(self):
        text = '```\n{"a": 1}\n```'
        assert strip_json_markdown(text) == '{"a": 1}'

    def test_empty(self):
        assert strip_json_markdown("") == ""


class TestParseScore:
    def test_int(self):
        assert parse_score(8) == 8.0
        assert isinstance(parse_score(8), float)

    def test_float(self):
        assert parse_score(8.5) == 8.5

    def test_string_number(self):
        assert parse_score("8.5") == 8.5

    def test_string_with_slash(self):
        assert parse_score("8.5/10") == 8.5

    def test_invalid_string(self):
        assert parse_score("N/A") == "N/A"

    def test_none(self):
        assert parse_score(None) is None


class TestRepairMojibake:
    def test_normal_text_unchanged(self):
        assert repair_latin1_gbk_mojibake("中文文本") == "中文文本"

    def test_empty(self):
        assert repair_latin1_gbk_mojibake("") == ""

    def test_ascii_unchanged(self):
        assert repair_latin1_gbk_mojibake("hello") == "hello"

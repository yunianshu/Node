"""Regression tests for advisory heuristics and bounded manuscript revision.

LLM responses are fixtures: these tests verify orchestration and text preservation,
not literary quality. No real projects, model APIs or notifications are used.
"""
import json
from pathlib import Path

import pytest

from core.edit_diff import EditApplyError, apply_reviewed_edits
from core.novel_config import ensure_project_structure
from core.review_quality import DEFAULT_CRITICAL_DIMENSION_MIN_SCORES
from core.outline_quality_gate import aggregate_outline_reviews, detect_scene_density_issues
from pipeline import coordinator, outliner, outline_reviewer, polisher, reviewer, writer


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def project(tmp_path, monkeypatch):
    ensure_project_structure(tmp_path)
    dump(tmp_path / "config.json", {"total_chapters": 5, "reviewer": {"semantic_retries": 0}})
    dump(tmp_path / "world.json", {"title": "回归样本", "themes": ["日常"]})
    dump(tmp_path / "characters.json", {"protagonist": {"name": "林川"}})
    outline = {
        "chapter_number": 4, "title": "等候", "summary": "林川留下等候，回想约定并决定留到天亮。" * 9,
        "characters_involved": ["林川"], "location": "院内", "mood": "平稳",
        "key_events": ["林川决定留在原地等到天亮。"], "foreshadowing": "本章无新增伏笔",
        "power_progression": "本章能力未变", "chapter_hook": "林川仍留在原处等候",
        "emotional_arc": "平稳", "tension_points": [], "story_beat": "transition",
        "chapter_goal": "林川需要在约定地点等候，以免错过已经约好的见面。",
        "payoff_design": "读者了解林川如何看待约定以及他为何愿意继续等待。",
        "human_anchor": "", "content_layers": ["等候时的认识变化"],
        "main_antagonist": "没有直接对手", "time_progression": "同一夜晚", "main_arc_link": "承接既定约定",
        "word_count_target": 5000, "scenes": [{"position": "opening", "objective": "留在原处等候"}],
    }
    dump(tmp_path / "chapters/outline/chapter_0004.json", outline)
    for module in (writer, reviewer, polisher, outliner, outline_reviewer, coordinator):
        module.init_project(tmp_path)
    # Prevent an accidental unstubbed external LLM call in all tests.
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected external model call")
    for module in (writer, reviewer, polisher, outline_reviewer):
        monkeypatch.setattr(module, "call_llm", forbidden)
    return tmp_path, outline


def report(score=8.8, verdict="通过", edits=None):
    return {
        "chapter_number": 4, "overall_score": score, "verdict": verdict,
        "status": "completed", "scores": {key: score for key in DEFAULT_CRITICAL_DIMENSION_MIN_SCORES},
        "strengths": ["人物行动可理解"], "weaknesses": [], "suggestions": [],
        "continuity_issues": [], "summary": "完成本章等候与认识变化的作用", "edits": edits or [],
    }


def manuscript():
    return "林川留在原处。\n\n" + "\n\n".join(
        f"第{i}次记下时辰。" + f"这一段的记号是{i}，约定的时间尚未过去。" * 9
        for i in range(30)
    ) + "\n\n他仍在等。"


def test_exact_patch_preserves_all_surrounding_text_and_line_endings():
    original = "前文 \r\n" * 40 + "独一处错误。" + "\r\n 后文" * 40
    edit = {"type": "replace", "old": "独一处错误。", "new": "这一处修正。"}
    assert apply_reviewed_edits(original, [edit], [edit]) == original.replace(edit["old"], edit["new"])


@pytest.mark.parametrize("edits,approved", [
    ([{"type": "replace", "old": "错处。", "new": "改好。"}], [{"type": "replace", "old": "不存在。", "new": "改好。"}]),
    ([{"type": "replace", "old": "旁边。", "new": "改好。"}], [{"type": "replace", "old": "错处。", "new": "改好。"}]),
    ([{"type": "insert", "after": "错处。", "text": "很多字" * 100}], [{"type": "insert", "after": "错处。", "text": "一句话"}]),
    ([], [{"type": "delete", "old": "错处。"}]),
])
def test_unsafe_or_unreviewed_patch_rejected(edits, approved):
    with pytest.raises(EditApplyError):
        apply_reviewed_edits("开头" * 60 + "错处。旁边。" + "结尾" * 60, edits, approved)


def test_equal_length_whole_chapter_rewrite_rejected():
    original = "原文" * 100
    edit = {"type": "replace", "old": original, "new": "新文" * 100}
    with pytest.raises(EditApplyError, match="修改量"):
        apply_reviewed_edits(original, [edit], [edit])


def test_duplicate_anchors_and_overlap_rejected():
    duplicate = {"type": "replace", "old": "重复。", "new": "修改。"}
    with pytest.raises(EditApplyError, match="不唯一"):
        apply_reviewed_edits("重复。重复。", [duplicate], [duplicate])
    edit = {"type": "delete", "old": "独一处。"}
    with pytest.raises(EditApplyError, match="重叠"):
        apply_reviewed_edits("填充" * 100 + "独一处。", [edit, edit], [edit])


def test_overlong_patch_is_rejected_instead_of_truncating_ending():
    original = "开头" * 100 + "独一处。结尾必须保留。"
    edit = {"type": "insert", "after": "独一处。", "text": "新增内容。"}
    with pytest.raises(EditApplyError, match="禁止截断"):
        apply_reviewed_edits(original, [edit], [edit], max_words=len(original))


def test_quiet_single_scene_outline_valid(project):
    _, outline = project
    assert outliner._validate_chapter_outline(outline, 4) == []
    assert detect_scene_density_issues(outline) is None
    assert outline["tension_points"] == []  # No synthetic three-point curve.
    assert detect_scene_density_issues({"scenes": []}) is not None


def test_repeated_sensory_object_is_not_a_design_failure():
    scene = {"position": "middle", "objective": "等候答复", "sensory_anchor": "同一盏灯"}
    assert detect_scene_density_issues({"scenes": [scene, scene]}) is None


def test_scored_examples_disabled_by_default(project):
    root, _ = project
    dump(root / "chapters/review/chapter_0001_review.json", {"overall_score": 9.9})
    (root / "chapters/final/chapter_0001.txt").write_text("不应自动模仿的高分稿", encoding="utf-8")
    assert writer._load_9star_examples(4) == ""


def test_outline_review_and_vote_allow_quiet_chapter(project, monkeypatch):
    _, _ = project
    response = report()
    response["design_gates"] = {
        "core_desire": {"passed": True, "evidence": "林川在等约好的人"},
        "scene_design": {"passed": True, "evidence": "单场景有具体等候目标"},
        "strong_hook": {"passed": False, "evidence": "安静收尾，本章不适用"},
        "midpoint_reversal": {"passed": False, "evidence": "本章无需反转"},
        "human_warmth": {"passed": False, "evidence": "本章没有生活物件"},
    }
    monkeypatch.setattr(outline_reviewer, "call_llm", lambda *a, **k: json.dumps(response, ensure_ascii=False))
    result = outline_reviewer.review_outline(4)
    assert result["design_gate_passed"] and result["verdict"] == "通过"
    aggregate = aggregate_outline_reviews(
        4, [result, result], min_score=8.5, required_rounds=2,
        required_votes=2, max_score_spread=1.0,
    )
    assert aggregate["verdict"] == "通过"
    result["design_gates"]["core_desire"]["passed"] = False
    aggregate = aggregate_outline_reviews(
        4, [result, result], min_score=8.5, required_rounds=2,
        required_votes=2, max_score_spread=1.0,
    )
    assert aggregate["verdict"] != "通过"


def test_keyword_failures_do_not_override_semantic_review(project, monkeypatch):
    root, _ = project
    text = manuscript()
    draft = root / "chapters/draft/chapter_0004.txt"
    draft.write_text(text, encoding="utf-8", newline="")
    # 人情味、关系与场景词匹配仍为提示；AI硬门单独按发布契约验证。
    monkeypatch.setattr(reviewer, "detect_ai_flavor", lambda *a, **k: {"ai_flavor_score": 9, "issues": []})
    monkeypatch.setattr(reviewer, "detect_human_warmth", lambda *a: {"passed": False, "issues": ["没有生活词"]})
    monkeypatch.setattr(reviewer, "detect_relationship_obligation", lambda *a: {"required": True, "passed": False})
    monkeypatch.setattr(reviewer, "detect_scene_realization", lambda *a, **k: {"required": True, "needs_attention": True, "rate": 0})
    def review_response(system, prompt, **kwargs):
        assert text in prompt  # Includes everything between the old sampled sections.
        return json.dumps(report(), ensure_ascii=False)
    monkeypatch.setattr(reviewer, "call_llm", review_response)
    result = reviewer.review_chapter(4)
    assert result["status"] == "completed"
    assert result["verdict"] == "通过" and result["overall_score"] == 8.8
    assert result["local_analysis"]["human_warmth_detection"]["advisory_only"]


@pytest.mark.parametrize("fault", ["short", "missing_character", "semantic_conflict"])
def test_actual_failures_still_block_high_score(project, monkeypatch, fault):
    root, _ = project
    text = "林川等候。" if fault == "short" else manuscript()
    if fault == "missing_character":
        text = text.replace("林川", "某人")
    (root / "chapters/draft/chapter_0004.txt").write_text(text, encoding="utf-8", newline="")
    edit = {"type": "replace", "old": text[:20], "new": "修正后的现场。"}
    response = report(9.5, "需修改" if fault == "semantic_conflict" else "通过", [edit])
    response.update(weaknesses=["存在事实矛盾"], suggestions=["修正该处事实"], continuity_issues=["既定事实被推翻"])
    monkeypatch.setattr(reviewer, "call_llm", lambda *a, **k: json.dumps(response, ensure_ascii=False))
    result = reviewer.review_chapter(4)
    assert result["overall_score"] < 8.5 and result["verdict"] != "通过"


@pytest.mark.parametrize("module", [writer, polisher])
@pytest.mark.parametrize("valid", [False, True])
def test_revision_never_changes_unreviewed_text(project, monkeypatch, module, valid):
    root, _ = project
    original = manuscript().replace("\n", "\r\n")
    draft = root / "chapters/draft/chapter_0004.txt"
    draft.write_bytes(original.encode("utf-8"))
    approved = {"type": "replace", "old": "林川留在原处。", "new": "林川仍留在原处。"}
    review = report(8.0, "需修改", [approved])
    review.update(weaknesses=["起句需要修正"], suggestions=["只修正起句"])
    dump(root / "chapters/review/chapter_0004_review.json", review)
    operation = approved if valid else {"type": "replace", "old": "他仍在等。", "new": "另外的结尾。"}
    calls = []
    def reply(*args, **kwargs):
        calls.append(1)
        return json.dumps({"edits": [operation]}, ensure_ascii=False)
    monkeypatch.setattr(module, "call_llm", reply)
    action = writer.generate_chapter if module is writer else polisher.polish_chapter
    result = action(4)
    expected = original.replace(approved["old"], approved["new"]) if valid else original
    assert draft.read_bytes() == expected.encode("utf-8")
    assert result == ("success" if valid else "failed")
    assert len(calls) == 1  # No full rewrite or second deepening call.


def test_coordinator_one_chapter_with_mock_models(project, monkeypatch):
    root, _ = project
    text = manuscript()
    monkeypatch.setattr(writer, "call_llm", lambda *a, **k: text)
    monkeypatch.setattr(reviewer, "call_llm", lambda *a, **k: json.dumps(report(), ensure_ascii=False))
    calls = []
    def run_script(name, *args):
        calls.append(name)
        assert name in {"writer.py", "reviewer.py"}
        if name == "writer.py":
            return 0 if writer.generate_chapter(4) == "success" else 1
        return 0 if reviewer.review_chapter(4)["status"] == "completed" else 1
    monkeypatch.setattr(coordinator, "run_script", run_script)
    monkeypatch.setattr(coordinator, "ensure_outline_lookahead", lambda *args: (True, None))
    monkeypatch.setattr(coordinator, "_extract_post_chapter_states", lambda *args: None)
    assert coordinator.run_serial_quality_workflow(4, 4)
    assert calls == ["writer.py", "reviewer.py"]
    assert (root / "chapters/final/chapter_0004.txt").read_text(encoding="utf-8") == text

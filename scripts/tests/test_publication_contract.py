"""发布契约回归：使用隔离项目，禁止触碰真实小说与通知。"""
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import ai_flavor_detector as ai
from core.novel_config import extract_origin_fact_terms, extract_origin_fact_clauses
from pipeline import coordinator, draft_gate, reviewer
from tests.test_narrative_revision import project, report, manuscript, dump


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def valid_report(text, **kwargs):
    return {**report(**kwargs), 'content_sha256': digest(text),
            'quality_gate': {'passed': True, 'reasons': []}}


def test_origin_real_runtime_paths():
    material = '## origin/facts\n### origin/facts/设定.txt\n- 林川在青石镇守护祖传药铺。\n## origin/style\n不可混入的风格词'
    assert any('林川' in item for item in extract_origin_fact_terms(material))
    assert extract_origin_fact_clauses(material) == ['林川在青石镇守护祖传药铺']
    assert '不可混入' not in str(extract_origin_fact_terms(material))
    assert '不可混入' not in str(extract_origin_fact_clauses(material))


@pytest.mark.parametrize('kind', ['parallel_sentiment', 'summary_ending', 'meta_narration'])
@pytest.mark.parametrize('score', [7.0, 8.5, 10.0])
def test_shared_ai_fingerprint_gate(kind, score):
    assert not ai.evaluate_ai_gate({'ai_flavor_score': score, 'issues': [{'type': kind, 'severity': 'major'}]})['passed']


@pytest.mark.parametrize('score', [None, '8.5', float('nan'), float('inf'), True, -1, 11])
def test_invalid_ai_score_closed(score):
    assert not ai.evaluate_ai_gate({'ai_flavor_score': score, 'issues': []})['passed']


def test_ai_boundary():
    assert ai.evaluate_ai_gate({'ai_flavor_score': 7.0, 'issues': []})['passed']
    assert not ai.evaluate_ai_gate({'ai_flavor_score': 6.99, 'issues': []})['passed']


@pytest.mark.parametrize('response,status,code', [('', 'failed', 1), ('{', 'parse_error', 1),
    ('{}', 'invalid_review', 1), ('[]', 'parse_error', 1)])
def test_reviewer_cli_execution_status(project, monkeypatch, response, status, code):
    root, _ = project
    (root / 'chapters/draft/chapter_0004.txt').write_text(manuscript(), encoding='utf-8')
    monkeypatch.setattr(reviewer, 'call_llm', lambda *a, **k: response)
    monkeypatch.setattr(sys, 'argv', ['reviewer', '--project', str(root), '--chapter', '4'])
    assert reviewer.main() == code
    assert json.loads((root / 'chapters/review/chapter_0004_review.json').read_text(encoding='utf-8'))['status'] == status


def test_valid_low_score_returns_zero_but_blocks(project, monkeypatch):
    root, _ = project
    text = manuscript()
    (root / 'chapters/draft/chapter_0004.txt').write_text(text, encoding='utf-8')
    response = report(7.5, '需修改', [{'type': 'replace', 'old': '林川留在原处。', 'new': '林川仍在等候。'}])
    response.update(weaknesses=['起句含糊'], suggestions=['修正起句'])
    monkeypatch.setattr(reviewer, 'call_llm', lambda *a, **k: json.dumps(response, ensure_ascii=False))
    monkeypatch.setattr(sys, 'argv', ['reviewer', '--project', str(root), '--chapter', '4'])
    assert reviewer.main() == 0
    assert not draft_gate.draft_gate_passed(coordinator.runtime_context(), 4)


def test_hash_uses_submitted_snapshot(project, monkeypatch):
    root, _ = project
    text = manuscript()
    path = root / 'chapters/draft/chapter_0004.txt'
    path.write_text(text, encoding='utf-8', newline='')
    def response(*a, **k):
        assert text in a[1]
        path.write_text(text + '正文已变化。', encoding='utf-8')
        return json.dumps(report())
    monkeypatch.setattr(reviewer, 'call_llm', response)
    result = reviewer.review_chapter(4)
    assert result['content_sha256'] == digest(text)
    assert not draft_gate.promote_draft_to_final(coordinator.runtime_context(), 4)


@pytest.mark.parametrize('cache', ['missing_hash', 'changed_text'])
def test_old_report_reaudited(project, monkeypatch, cache):
    root, _ = project
    text = manuscript()
    (root / 'chapters/draft/chapter_0004.txt').write_text(text, encoding='utf-8', newline='')
    old = valid_report(text)
    if cache == 'missing_hash':
        old.pop('content_sha256')
    else:
        old['content_sha256'] = digest('别的正文')
    dump(root / 'chapters/review/chapter_0004_review.json', old)
    call = Mock(return_value=json.dumps(report()))
    monkeypatch.setattr(reviewer, 'call_llm', call)
    assert reviewer.review_chapter(4)['content_sha256'] == digest(text)
    call.assert_called_once()


@pytest.mark.parametrize('status,verdict', [('completed', '需修改'), ('invalid_review_salvaged', '通过')])
def test_high_score_cannot_publish(project, status, verdict):
    root, _ = project
    text = manuscript()
    (root / 'chapters/draft/chapter_0004.txt').write_text(text, encoding='utf-8', newline='')
    data = valid_report(text, score=9.8, verdict=verdict)
    data['status'] = status
    dump(root / 'chapters/review/chapter_0004_review.json', data)
    assert not draft_gate.promote_draft_to_final(coordinator.runtime_context(), 4)
    assert not (root / 'chapters/final/chapter_0004.txt').exists()


def test_ai_detection_error_preserves_final(project, monkeypatch):
    root, _ = project
    text = manuscript()
    (root / 'chapters/draft/chapter_0004.txt').write_text(text, encoding='utf-8', newline='')
    final = root / 'chapters/final/chapter_0004.txt'
    final.write_bytes(b'previous final')
    dump(root / 'chapters/review/chapter_0004_review.json', valid_report(text))
    monkeypatch.setattr(ai, 'detect_ai_flavor', Mock(side_effect=RuntimeError('检测失败')))
    assert not draft_gate.promote_draft_to_final(coordinator.runtime_context(), 4)
    assert final.read_bytes() == b'previous final'


def test_post_write_failure_stops_state_and_progress(project, monkeypatch):
    monkeypatch.setattr(coordinator, 'ensure_outline_lookahead', lambda *a: (True, None))
    monkeypatch.setattr(coordinator, 'process_draft_gate', lambda *a: True)
    monkeypatch.setattr(coordinator, '_final_ai_flavor_check', lambda *a: False)
    states, progress = Mock(), Mock()
    monkeypatch.setattr(coordinator, '_extract_post_chapter_states', states)
    monkeypatch.setattr(coordinator, 'refresh_progress_from_status', progress)
    assert not coordinator.run_serial_quality_workflow(4, 4)
    states.assert_not_called()
    progress.assert_not_called()


@pytest.mark.parametrize('branch', ['polisher_failed', 'score_regression', 'exhausted'])
def test_best_draft_fallback_reaudits(project, monkeypatch, branch):
    root, _ = project
    text = manuscript()
    draft = root / 'chapters/draft/chapter_0004.txt'
    draft.write_text(text, encoding='utf-8', newline='')
    coordinator._save_best_draft(4, 9.8, draft)
    data = valid_report(text, score=8.6, verdict='需修改')
    dump(coordinator._review_file(4), data)
    coordinator.CONFIG['polisher'].update(enabled=branch != 'exhausted', max_attempts=1)
    coordinator.CONFIG['coordinator'].update(draft_analysis_rounds=1, draft_attempts_per_round=1)
    calls = []
    def run(name, *args):
        calls.append((name, args))
        if name == 'polisher.py':
            if branch == 'polisher_failed':
                return 1
            draft.write_text(text + '修改未提升。', encoding='utf-8', newline='')
            return 0
        if name == 'writer.py':
            return 1
        assert name == 'reviewer.py'
        current = draft.read_bytes().decode('utf-8')
        dump(coordinator._review_file(4), valid_report(current, score=8.0, verdict='需修改'))
        return 0
    monkeypatch.setattr(coordinator, 'run_script', run)
    monkeypatch.setattr(coordinator, '_push_gate_failure', lambda *a: None)
    rt = coordinator.runtime_context()
    if branch == 'exhausted':
        assert not draft_gate.process_draft_gate(rt, 4)
    else:
        assert not draft_gate._try_polish_pass(rt, 4, 1, 1, [])
    reaudits = [args for name, args in calls if name == 'reviewer.py' and '--force' in args]
    assert reaudits
    assert not coordinator._final_file(4).exists()


@pytest.mark.parametrize('kind', ['parallel_sentiment', 'summary_ending', 'meta_narration'])
@pytest.mark.parametrize('score', [7.0, 8.5, 10.0])
def test_reviewer_uses_shared_ai_gate(project, monkeypatch, kind, score):
    root, _ = project
    text = manuscript()
    (root / 'chapters/draft/chapter_0004.txt').write_text(text, encoding='utf-8', newline='')
    monkeypatch.setattr(reviewer, 'detect_ai_flavor', lambda *a, **k: {'ai_flavor_score': score, 'issues': [{'type': kind, 'severity': 'major'}]})
    response = report(9.8, edits=[{'type': 'replace', 'old': '林川留在原处。', 'new': '林川仍在等候。'}])
    response.update(weaknesses=['句式问题'], suggestions=['修正句式'])
    monkeypatch.setattr(reviewer, 'call_llm', lambda *a, **k: json.dumps(response, ensure_ascii=False))
    result = reviewer.review_chapter(4)
    assert not result['quality_gate']['passed']
    assert result['verdict'] != '通过'
    assert kind in str(result['quality_gate']['reasons'])


@pytest.mark.parametrize('status', ['failed', 'no_file', 'parse_error', 'invalid_review', 'invalid_review_salvaged'])
def test_cli_all_failed_statuses_nonzero(project, monkeypatch, status):
    root, _ = project
    monkeypatch.setattr(reviewer, 'review_chapter', lambda *a, **k: {'status': status})
    monkeypatch.setattr(sys, 'argv', ['reviewer', '--project', str(root), '--chapter', '4'])
    assert reviewer.main() == 1


def test_reviewer_failure_feedback_reaches_next_writer(project, monkeypatch):
    root, _ = project
    coordinator.CONFIG['coordinator'].update(draft_analysis_rounds=2, draft_attempts_per_round=1)
    coordinator.CONFIG['polisher']['enabled'] = False
    writer_calls = []
    def run(name, *args):
        if name == 'writer.py':
            writer_calls.append(args)
            coordinator._draft_file(4).write_text(manuscript(), encoding='utf-8', newline='')
            if len(writer_calls) == 2:
                assert '--review-feedback' in args
                payload = json.loads(Path(args[args.index('--review-feedback') + 1]).read_text(encoding='utf-8'))
                latest = payload['4']['reviews'][-1]
                assert latest['execution_failed'] is True
                assert latest['report_status'] == 'invalid_review'
                assert latest['review_contract_errors'] == ['契约缺少编辑']
                assert latest['quality_gate']['reasons'] == ['AI指纹']
                assert latest['repair_tasks'] == ['修正结尾']
            return 0
        dump(coordinator._review_file(4), {'status': 'invalid_review', 'review_contract_errors': ['契约缺少编辑'],
             'quality_gate': {'passed': False, 'reasons': ['AI指纹']}, 'repair_tasks': ['修正结尾']})
        return 1
    monkeypatch.setattr(coordinator, 'run_script', run)
    monkeypatch.setattr(coordinator, '_push_gate_failure', lambda *a: None)
    assert not draft_gate.process_draft_gate(coordinator.runtime_context(), 4)
    assert len(writer_calls) == 2


def test_valid_high_score_revision_returns_zero_without_auto_pass(project, monkeypatch):
    root, _ = project
    text = manuscript()
    coordinator._draft_file(4).write_text(text, encoding='utf-8', newline='')
    response = report(9.9, '需修改', [{'type': 'replace', 'old': '林川留在原处。', 'new': '林川仍在等候。'}])
    response.update(weaknesses=['事实矛盾'], suggestions=['修正事实'])
    monkeypatch.setattr(reviewer, 'call_llm', lambda *a, **k: json.dumps(response, ensure_ascii=False))
    monkeypatch.setattr(sys, 'argv', ['reviewer', '--project', str(root), '--chapter', '4'])
    assert reviewer.main() == 0
    assert coordinator._load_json_file(coordinator._review_file(4))['verdict'] == '需修改'
    assert not draft_gate.promote_draft_to_final(coordinator.runtime_context(), 4)


def test_origin_materials_enter_writer_and_reviewer_paths(project, monkeypatch):
    from pipeline import writer
    root, _ = project
    fact_dir = root / 'origin/facts'
    fact_dir.mkdir()
    (fact_dir / '设定.txt').write_text('林川在青石镇守护祖传药铺。', encoding='utf-8')
    reviewer.init_project(root)
    writer.init_project(root)
    text = manuscript()
    def generate(system, prompt, **kwargs):
        assert '林川在青石镇守护祖传药铺' in prompt
        return text
    monkeypatch.setattr(writer, 'call_llm', generate)
    assert writer.generate_chapter(4) == 'success'
    def review(system, prompt, **kwargs):
        assert '林川在青石镇守护祖传药铺' in prompt
        return json.dumps(report(), ensure_ascii=False)
    monkeypatch.setattr(reviewer, 'call_llm', review)
    result = reviewer.review_chapter(4)
    assert result['status'] == 'completed'
    assert result['local_analysis']['origin_fact_reference_detection']['fact_terms_sample']


@pytest.mark.parametrize('stage', ['planner', 'media', 'draft', 'book'])
def test_coordinator_main_failure_exit_codes(project, monkeypatch, stage):
    root, _ = project
    monkeypatch.setattr(coordinator, 'init_project', lambda *a: None)
    coordinator.CONFIG['media']['enabled'] = stage == 'media'
    monkeypatch.setattr(coordinator, 'ensure_wechat_pusher_process', lambda *a: None)
    monkeypatch.setattr(coordinator, 'ensure_gate_watchdog_processes', lambda *a: None)
    monkeypatch.setattr(coordinator, 'refresh_progress_from_status', lambda *a: {'last_generated_chapter': 0, 'last_reviewed_chapter': 0})
    monkeypatch.setattr(coordinator, 'save_progress', lambda *a: None)
    monkeypatch.setattr(coordinator, 'run_planner', lambda *a: 1 if stage == 'planner' else 0)
    monkeypatch.setattr(coordinator, 'media_prompts_ready', lambda: True)
    monkeypatch.setattr(coordinator, 'run_media_generator', lambda: 1)
    monkeypatch.setattr(coordinator, 'run_serial_quality_workflow', lambda *a: stage != 'draft')
    monkeypatch.setattr(coordinator, 'process_book_review_gate', lambda **k: False)
    monkeypatch.setattr(coordinator, '_auto_resume_book_review_repair', lambda **k: False)
    monkeypatch.setattr(coordinator, 'generate_summary_report', lambda: {})
    monkeypatch.setattr(coordinator, 'run_story_flow_audit_report', lambda *a: None)
    complete = Mock()
    monkeypatch.setattr(coordinator, '_push_task_complete', complete)
    monkeypatch.setattr(sys, 'argv', ['coordinator', '--project', str(root), '--start', '4', '--end', '4'])
    assert coordinator.main() == 1
    complete.assert_not_called()


def test_post_write_detector_failure_rolls_back_and_persists_block(project, monkeypatch):
    root, _ = project
    text = manuscript()
    coordinator._draft_file(4).write_text(text, encoding='utf-8', newline='')
    final = coordinator._final_file(4)
    final.write_text('原有终稿。', encoding='utf-8', newline='')
    dump(coordinator._review_file(4), valid_report(text))
    monkeypatch.setattr(ai, 'detect_ai_flavor', Mock(side_effect=[
        {'ai_flavor_score': 10, 'issues': []}, RuntimeError('复检失败')]))
    assert not draft_gate.promote_draft_to_final(coordinator.runtime_context(), 4)
    assert final.read_text(encoding='utf-8') == '原有终稿。'
    assert coordinator._load_json_file(coordinator._review_file(4))['quality_gate']['passed'] is False


def test_final_recheck_fails_persistently_before_progress(project, monkeypatch):
    from core.workflow_state import scan_one_chapter
    root, _ = project
    text = manuscript()
    coordinator._draft_file(4).write_text(text, encoding='utf-8', newline='')
    coordinator._final_file(4).write_text(text, encoding='utf-8', newline='')
    dump(coordinator._review_file(4), valid_report(text))
    monkeypatch.setattr(ai, 'detect_ai_flavor', lambda *a, **k: {'ai_flavor_score': 8.5, 'issues': [{'type': 'meta_narration'}]})
    assert coordinator._final_ai_flavor_check(4) is False
    monkeypatch.setattr(ai, 'detect_ai_flavor', lambda *a, **k: {'ai_flavor_score': 10, 'issues': []})
    assert not scan_one_chapter(root, 4).final_ok


@pytest.mark.parametrize('branch', ['polisher_failed', 'score_regression', 'exhausted'])
def test_restored_best_can_publish_only_after_successful_fresh_review(project, monkeypatch, branch):
    root, _ = project
    text = manuscript()
    coordinator._draft_file(4).write_text(text, encoding='utf-8', newline='')
    coordinator._save_best_draft(4, 9.8, coordinator._draft_file(4))
    dump(coordinator._review_file(4), valid_report(text, score=8.6, verdict='需修改'))
    coordinator.CONFIG['polisher'].update(enabled=branch != 'exhausted', max_attempts=1)
    coordinator.CONFIG['coordinator'].update(draft_analysis_rounds=1, draft_attempts_per_round=1)
    restored_reviews = []
    def run(name, *args):
        if name == 'writer.py':
            return 1
        if name == 'polisher.py':
            if branch == 'polisher_failed':
                return 1
            coordinator._draft_file(4).write_text(text + '精修仍有问题。', encoding='utf-8', newline='')
            return 0
        assert '--force' in args
        current = coordinator._draft_file(4).read_bytes().decode('utf-8')
        # 用正文内容确认确实在恢复历史稿之后重审。
        restored = current.replace('\r\n', '\n') == text
        # 耗尽路径的existing_draft预审先拒绝，最后恢复后才通过。
        if branch == 'exhausted' and not restored_reviews:
            restored_reviews.append(False)
            dump(coordinator._review_file(4), valid_report(current, score=8, verdict='需修改'))
        else:
            restored_reviews.append(restored)
            dump(coordinator._review_file(4), valid_report(current, score=9.8 if restored else 8,
                 verdict='通过' if restored else '需修改'))
        return 0
    monkeypatch.setattr(coordinator, 'run_script', run)
    rt = coordinator.runtime_context()
    if branch == 'exhausted':
        assert draft_gate.process_draft_gate(rt, 4)
    else:
        assert draft_gate._try_polish_pass(rt, 4, 1, 1, [])
        assert draft_gate.promote_draft_to_final(rt, 4)
    assert restored_reviews[-1] is True
    assert coordinator._final_file(4).exists()
    assert coordinator._load_json_file(coordinator._review_file(4))['content_sha256'] == digest(coordinator._final_file(4).read_bytes().decode('utf-8'))

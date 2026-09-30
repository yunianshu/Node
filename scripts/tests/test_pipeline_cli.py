"""真实 CLI/子进程测试：本地 HTTP 固定响应仅验证控制流，不冒充真实模型。"""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest

from tests.test_narrative_revision import manuscript, report, dump
from core.novel_config import ensure_project_structure

ROOT = Path(__file__).resolve().parents[2]


@contextlib.contextmanager
def model_server(response):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(body)
            self.send_response(503 if response == 'HTTP_ERROR' else 200)
            self.end_headers()
            content = response(body) if callable(response) else response
            self.wfile.write(json.dumps({'choices': [{'message': {'content': content}}]}).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def make_config(url):
    return {'total_chapters': 5, 'media': {'enabled': False},
            'coordinator': {'background_monitors_enabled': False, 'draft_attempts_per_round': 1, 'draft_analysis_rounds': 1},
            'writer': {'provider': 'openai_compatible', 'model': 'fixture-writer', 'base_url': url, 'api_key': 'fixture', 'max_retries': 0},
            'reviewer': {'provider': 'openai_compatible', 'model': 'fixture-review', 'base_url': url, 'api_key': 'fixture', 'semantic_retries': 0},
            'webhook_url': ''}


def cli(script, *args, cwd=ROOT):
    env = {key: value for key, value in os.environ.items() if not key.startswith('NOVEL_') and key not in {'PYTHONHOME', 'PYTHONPATH'}}
    env['PYTHONPATH'] = str(ROOT / 'scripts')
    return subprocess.run([sys.executable, str(cwd / script), *map(str, args)], cwd=cwd,
                          capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=45)


@pytest.mark.parametrize('response,status,rc', [('HTTP_ERROR', 'failed', 1), ('{', 'parse_error', 1), ('{}', 'invalid_review', 1)])
def test_reviewer_real_cli_errors(tmp_path, response, status, rc):
    with model_server(response) as (url, calls):
        ensure_project_structure(tmp_path)
        dump(tmp_path / 'config.json', make_config(url))
        (tmp_path / 'chapters/draft/chapter_0004.txt').write_text(manuscript(), encoding='utf-8', newline='')
        result = cli('scripts/pipeline/reviewer.py', '--project', tmp_path, '--chapter', 4)
        assert result.returncode == rc, result.stdout + result.stderr
        data = json.loads((tmp_path / 'chapters/review/chapter_0004_review.json').read_text(encoding='utf-8'))
        assert data['status'] == status
        assert data['content_sha256']
        assert calls and calls[0]['model'] == 'fixture-review'


def test_reviewer_cli_batch_one_missing_fails(tmp_path):
    with model_server(json.dumps(report())) as (url, calls):
        ensure_project_structure(tmp_path)
        dump(tmp_path / 'config.json', make_config(url))
        (tmp_path / 'chapters/draft/chapter_0004.txt').write_text(manuscript(), encoding='utf-8', newline='')
        result = cli('scripts/pipeline/reviewer.py', '--project', tmp_path, '--start', 4, '--end', 5)
        assert result.returncode == 1
        assert json.loads((tmp_path / 'chapters/review/chapter_0004_review.json').read_text(encoding='utf-8'))['status'] == 'completed'


@pytest.mark.parametrize('entry', ['_gen_serial.py', 'scripts/maintenance/gen_serial_watchdog.py'])
def test_legacy_real_child_coordinator_failure(tmp_path, entry):
    ensure_project_structure(tmp_path)
    dump(tmp_path / 'config.json', make_config('http://127.0.0.1:1/v1'))
    args = ['--project', tmp_path]
    if 'watchdog' in entry:
        args += ['--once', '--', '--skip-planner']
    else:
        args += ['--skip-planner']
    result = cli(entry, *args)
    assert result.returncode == 1, result.stdout + result.stderr
    log = (tmp_path / 'logs/coordinator.log').read_text(encoding='utf-8')
    assert 'world.json' in log and 'characters.json' in log


@pytest.mark.parametrize('args', [['--candidates', '4'], ['--workers', '4'], ['--timeout', '600'], ['--time-limit', '10']])
def test_unsupported_legacy_options_rejected(args):
    result = cli('_gen_serial.py', *args)
    assert result.returncode == 2
    assert '无法映射' in result.stderr or '无等价映射' in result.stderr


def test_legacy_argument_mapping_real_subprocess(tmp_path):
    for relative in ['_gen_serial.py', 'scripts/maintenance/gen_serial.py']:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    child = tmp_path / 'scripts/pipeline/coordinator.py'
    child.parent.mkdir(parents=True, exist_ok=True)
    child.write_text("import json,sys\nfrom pathlib import Path\nPath('argv.json').write_text(json.dumps(sys.argv[1:]))\nraise SystemExit(17)\n", encoding='utf-8')
    result = cli('_gen_serial.py', '--start', 4, '--end', 5, '--max-rounds', 3, '--candidates', 1, '--workers', 1, cwd=tmp_path)
    assert result.returncode == 17
    assert json.loads((tmp_path / 'argv.json').read_text()) == ['--start', '4', '--end', '5', '--draft-rounds', '3']


def test_copied_project_single_chapter_cli_smoke(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'copy'
    ensure_project_structure(source)
    dump(source / 'world.json', {'title': '单章冒烟'})
    dump(source / 'characters.json', {'protagonist': {'name': '林川'}})
    dump(source / 'chapters/outline/chapter_0004.json', {'chapter_number': 4, 'title': '等候', 'characters_involved': ['林川']})
    dump(source / 'chapters/outline_review/chapter_0004_review.json',
         {'chapter_number': 4, 'status': 'completed', 'overall_score': 9.5, 'verdict': '通过',
          'scores': {}, 'summary': '通过', 'design_gate_passed': True})
    def response(body):
        if body['model'] == 'fixture-writer':
            return manuscript()
        # 跨章状态抽取也使用独立配置，这里只给空状态响应。
        if body['model'] != 'fixture-review':
            return '{}'
        return json.dumps(report(), ensure_ascii=False)
    with model_server(response) as (url, calls):
        config = make_config(url)
        for section in ['character_state', 'arc_state', 'relationship_state']:
            config[section] = {'provider': 'openai_compatible', 'model': 'fixture-state', 'base_url': url, 'api_key': 'fixture', 'max_retries': 0}
        dump(source / 'config.json', config)
        shutil.copytree(source, target)
        result = cli('scripts/pipeline/coordinator.py', '--project', target, '--start', 4, '--end', 4,
                     '--outline-lookahead', 1, '--skip-planner', '--skip-book-review')
        assert result.returncode == 0, result.stdout + result.stderr
        review = json.loads((target / 'chapters/review/chapter_0004_review.json').read_text(encoding='utf-8'))
        assert review['status'] == 'completed' and review['verdict'] == '通过'
        assert (target / 'chapters/final/chapter_0004.txt').read_bytes() == (target / 'chapters/draft/chapter_0004.txt').read_bytes()
        assert (target / 'chapters/final/chapter_0004.txt').read_text(encoding='utf-8') == manuscript()
        import hashlib
        assert review['content_sha256'] == hashlib.sha256((target / 'chapters/final/chapter_0004.txt').read_bytes()).hexdigest()
        assert not (source / 'chapters/final/chapter_0004.txt').exists()
        assert {'fixture-writer', 'fixture-review'} <= {call['model'] for call in calls}

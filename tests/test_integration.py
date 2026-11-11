"""Exercise the real adapters with a subprocess and an HTTP Jira emulator."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from improver.agent import Agent
from improver.cli import main
from improver.core import Config, Runner, State, TransientError
from improver.engine import Engine
from improver.git import Repository
from improver.jira import Jira
from test_git_agent import make_repo, git


@contextmanager
def server():
    class Handler(BaseHTTPRequestHandler):
        items = []
        limited = False
        requests_seen = []

        def log_message(self, *args):
            pass

        def handle_request(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or '{}')
            self.requests_seen.append((self.command, self.path, body))
            result, status = {}, 200
            if self.path == '/limited':
                self.send_response(429)
                self.send_header('Retry-After', '77')
                self.end_headers()
                self.wfile.write(b'{"error":"limited"}')
                return
            if self.path.endswith('/search/jql'):
                selected = self.items
                if 'self-improver-fp-' in body.get('jql', ''):
                    selected = [i for i in selected if any(label in body['jql'] for label in i['fields']['labels'] if 'fp-' in label)]
                result = {'issues': selected, 'isLast': True}
            elif self.command == 'POST' and self.path == '/rest/api/3/issue':
                fields = body['fields']
                fields.update(created=f'2026-01-{len(self.items)+1:02d}', status={'name': 'Open', 'statusCategory': {'key': 'new'}})
                issue = {'key': f'APP-{len(self.items)+1}', 'fields': fields}
                self.items.append(issue)
                result, status = {'key': issue['key']}, 201
            elif '/board/1/issue' in self.path:
                result = {'issues': self.items}
            elif '/backlog/' in self.path:
                status = 204
            elif '/transitions' in self.path:
                key = self.path.split('/issue/')[1].split('/')[0]
                issue = next(i for i in self.items if i['key'] == key)
                if self.command == 'GET':
                    result = {'transitions': [{'id': name, 'to': {'name': name}, 'fields': {}}
                                              for name in ('Open', 'Working', 'Done')]}
                else:
                    name = body['transition']['id']
                    issue['fields']['status'] = {'name': name, 'statusCategory': {'key': 'done' if name == 'Done' else 'indeterminate'}}
                    status = 204
            elif '/properties/' in self.path:
                status = 204
            elif '/issue/APP-' in self.path:
                key = self.path.split('/issue/')[1].split('/')[0]
                result = next(i for i in self.items if i['key'] == key)
                if self.command == 'PUT':
                    for update in body.get('update', {}).get('labels', []):
                        result['fields']['labels'].append(update['add'])
                    status = 204
            else:
                status, result = 404, {'error': self.path}
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            if status != 204:
                self.wfile.write(json.dumps(result).encode())

        do_GET = do_POST = do_PUT = handle_request

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    class LocalOpener:
        def open(self, request, timeout):
            path = request.full_url.split('https://fixture.invalid', 1)[1]
            local = Request(f'http://127.0.0.1:{http.server_port}{path}', data=request.data,
                            headers=dict(request.headers), method=request.method)
            return urlopen(local, timeout=timeout)
    try:
        yield LocalOpener(), Handler
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


class IntegrationTests(unittest.TestCase):
    def test_real_subprocess_and_http_complete_two_cycles(self):
        with tempfile.TemporaryDirectory(prefix='full integration ') as d, server() as (opener, handler):
            root = Path(d) / 'application'
            make_repo(root)
            (root / 'app.py').write_text('VALUE = 1\n')
            (root / '.gitignore').write_text('__pycache__/\n')
            (root / 'tests').mkdir()
            (root / 'tests' / 'test_app.py').write_text('import unittest, app\nclass Base(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(app.VALUE, 1)\n')
            git(root, 'add', '.')
            git(root, 'commit', '-m', 'application fixture')
            repo = Repository(root, Runner())
            repo.home.mkdir(parents=True)
            cfg = Config(app_id='fixture', codex=[sys.executable, str(Path(__file__).with_name('fake_codex.py'))],
                         jira={'base_url': 'https://fixture.invalid', 'project': 'APP', 'board_id': 1,
                               'issue_types': {'bug': '1', 'feature': '2', 'improvement': '3'},
                               'priorities': ['1', '2', '3'], 'custom_fields': {},
                               'statuses': {'backlog': 'Open', 'in_progress': 'Working', 'done': 'Done'}})
            with State(repo.home / 'state.db') as state, patch.dict(os.environ, {'JIRA_EMAIL': 'fixture@example.invalid', 'JIRA_API_TOKEN': 'fixture-secret'}):
                runner = Runner(state)
                agent = Agent(cfg, repo.home, runner)
                # Skill pin loading has separate tests; keep this integration fixture offline.
                agent.skills = lambda *args: 'Audited fixture skills'
                jira = Jira(cfg)
                jira.opener = opener
                engine = Engine(cfg, repo, state, jira, agent, runner)
                original = repo.head()
                engine.cycle()
                first = repo.head()
                engine.cycle()
                self.assertNotEqual(original, first)
                self.assertNotEqual(first, repo.head())
                self.assertEqual([i['fields']['summary'] for i in handler.items], ['Add greeting', 'Add farewell', 'Add shout'])
                self.assertEqual([i['fields']['status']['name'] for i in handler.items], ['Done', 'Done', 'Open'])
                self.assertIn('def farewell', (root / 'app.py').read_text())
                self.assertFalse(state.pending())
                self.assertEqual(git(root, 'status', '--porcelain'), '')
                transcripts = list((repo.home / 'sessions').glob('*/events.jsonl'))
                self.assertTrue(any('red_test_exit' in p.read_text() for p in transcripts))
                self.assertTrue(all('fixture-secret' not in p.read_text() for p in transcripts))

    def test_real_http_rate_limit_carries_retry_after(self):
        with server() as (opener, _), patch.dict(os.environ, {'JIRA_EMAIL': 'x', 'JIRA_API_TOKEN': 'x'}):
            jira = Jira(Config(jira={'base_url': 'https://fixture.invalid'}))
            jira.opener = opener
            with self.assertRaises(TransientError) as caught:
                jira.request('GET', '/limited')
            self.assertEqual(caught.exception.retry_after, 77)

    def test_cli_help(self):
        with self.assertRaises(SystemExit) as caught:
            main(['--help'])
        self.assertEqual(caught.exception.code, 0)


if __name__ == '__main__':
    unittest.main()

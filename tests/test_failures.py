from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from improver.agent import Agent, validate_schema, RECOMMENDATIONS_SCHEMA
from improver.cli import main
from improver.core import Blocked, Config, ConfigError, Runner, State, TransientError
from improver.git import Repository
from improver.jira import Jira, retry_seconds
import test_engine as engine_fixtures
from test_git_agent import make_repo, git
from test_jira import FakeTransport


class ProcessTests(unittest.TestCase):
    def test_relative_virtualenv_executable_resolves_from_worktree(self):
        import venv
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            venv.EnvBuilder(with_pip=False).create(root / '.venv')
            relative = '.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python'
            result = Runner().run([relative, '-c', 'print(123)'], root)
            self.assertEqual(result['stdout'].strip(), '123')

    def test_credentials_are_removed_from_children_and_redacted_from_evidence(self):
        with tempfile.TemporaryDirectory() as d, State(Path(d) / 'state.db') as state:
            with patch.dict(os.environ, {'JIRA_EMAIL': 'private@example.invalid', 'JIRA_API_TOKEN': 'sensitive-value'}):
                result = Runner(state).run([sys.executable, '-c',
                    'import os; assert not any(k.startswith("JIRA_") for k in os.environ); print("sensitive-value")'], d)
                self.assertIn('[REDACTED]', result['stdout'])
                self.assertNotIn('sensitive-value', json.dumps(state.recent()))

    def test_timeout_kills_child_process_tree(self):
        with tempfile.TemporaryDirectory() as d:
            child = Path(d) / 'child.pid'
            program = ('import subprocess,sys,time,pathlib; '
                       'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(120)"]); '
                       'pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(120)')
            with self.assertRaises(Blocked):
                Runner().run([sys.executable, '-c', program, str(child)], d, timeout=1)
            pid = int(child.read_text())
            if os.name == 'nt':
                output = subprocess.check_output(['tasklist', '/FI', f'PID eq {pid}', '/NH']).decode(errors='replace')
                self.assertNotIn(str(pid), output)
            else:
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)

    def test_keyboard_interrupt_returns_shutdown_exit_code(self):
        with patch('improver.cli.execute', side_effect=KeyboardInterrupt), redirect_stdout(io.StringIO()):
            self.assertEqual(main(['run']), 130)

    def test_schema_rejects_boolean_as_integer(self):
        with self.assertRaises(Blocked):
            validate_schema(True, {'type': 'integer'})

    def test_agent_rejects_invalid_json_and_retries_rate_limits(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            script = root / 'fake.py'
            script.write_text('import sys,pathlib\nsys.stdin.read()\npathlib.Path(sys.argv[sys.argv.index("-o")+1]).write_text("not-json")\n')
            agent = Agent(Config(codex=[sys.executable, str(script)]), root, Runner())
            with self.assertRaises(Blocked):
                agent.call('test', 'prompt', {'type': 'object'}, root, False)
            script.write_text('import sys\nsys.stdin.read()\nprint("429 rate limit",file=sys.stderr)\nsys.exit(1)\n')
            with self.assertRaises(TransientError):
                agent.call('test', 'prompt', {'type': 'object'}, root, False)


class PinAndCliTests(unittest.TestCase):
    def test_launcher_doctor_uses_only_launcher_settings_and_keeps_targets_separate(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'app'
            make_repo(root)
            repo = Repository(root, Runner())
            repo.home.mkdir(parents=True)
            legacy = repo.home / 'config.json'
            legacy.write_text('invalid JSON', encoding='utf-8')
            settings = Path(d) / 'setup-scrum.json'
            pin = 'a' * 40
            args = ['--repo', str(root), '--launcher-settings', str(settings), 'doctor']
            for project, board in [('OTHER', 82), ('SCRUM', 1)]:
                settings.write_text(json.dumps({'jira_url': 'https://fixture.invalid/', 'jira_project': project,
                                                 'jira_board_id': board, 'jira_api_token': 'fixture-secret'}),
                                    encoding='utf-8-sig')
                with patch('improver.cli.prepare_skills', return_value=(str(repo.home / 'agent-skills'), pin)), \
                        patch('improver.cli.codex_command', return_value=[sys.executable]), \
                        patch('improver.cli.configure_logging'), \
                        patch('improver.cli.doctor', return_value={'jira': 'OK'}) as doctor, \
                        redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(args), 0)
                cfg = doctor.call_args.args[0]
                self.assertEqual((cfg.jira['project'], cfg.jira['board_id']), (project, board))
                self.assertEqual(json.loads(output.getvalue())['launcher_settings'],
                                 {'jira_url': 'https://fixture.invalid', 'jira_project': project,
                                  'jira_board_id': board, 'skills_commit': pin})
                self.assertNotIn('fixture-secret', output.getvalue())
                if legacy.exists():
                    self.assertEqual(legacy.read_text(encoding='utf-8'), 'invalid JSON')
                    legacy.unlink()
            self.assertFalse(legacy.exists())
            self.assertEqual(len(list((repo.home / 'launcher').glob('*/state.db'))), 2)
            with patch('improver.cli.prepare_skills', return_value=(str(repo.home / 'agent-skills'), pin)), \
                    patch('improver.cli.codex_command', return_value=[sys.executable, '-c', 'pass']), \
                    patch('improver.cli.configure_logging'), patch.object(Jira, 'doctor'), patch.object(Agent, 'skills'), \
                    patch.object(engine_fixtures.Engine, 'cycle', autospec=True) as cycle:
                self.assertEqual(main([*args[:-1], 'run', '--once']), 0)
                self.assertEqual(cycle.call_args.args[0].config.jira['project'], 'SCRUM')
            self.assertFalse(legacy.exists())

    def test_init_pins_local_skills_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as d:
            root, skills = Path(d) / 'app', Path(d) / 'skills-source'
            make_repo(root)
            make_repo(skills)
            names = ['using-agent-skills', 'spec-driven-development', 'planning-and-task-breakdown',
                     'incremental-implementation', 'test-driven-development', 'git-workflow-and-versioning',
                     'documentation-and-adrs', 'security-and-hardening', 'code-review-and-quality']
            for name in names:
                path = skills / 'skills' / name / 'SKILL.md'
                path.parent.mkdir(parents=True)
                path.write_text(f'# {name}\nAudited fixture workflow.')
            (skills / 'references').mkdir()
            (skills / 'references' / 'definition-of-done.md').write_text('# Definition of Done')
            git(skills, 'add', '.')
            git(skills, 'commit', '-m', 'fixture skills')
            with patch('improver.cli.codex_command', return_value=[sys.executable]), redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--repo', str(root), 'init', '--skills-source', str(skills)]), 0)
            repo = Repository(root, Runner())
            cfg = Config.load(repo.home / 'config.json')
            self.assertEqual(cfg.skills_commit, git(skills, 'rev-parse', 'HEAD'))
            self.assertNotIn(str(repo.worktrees), str(repo.common))
            agent = Agent(cfg, repo.home, Runner())
            self.assertIn('Audited fixture', agent.skills('review'))
            changed = Path(cfg.skills_path) / 'skills' / 'using-agent-skills' / 'SKILL.md'
            changed.write_text('tampered')
            with self.assertRaises(ConfigError):
                agent.skills('review')
            with State(repo.home / 'state.db') as state:
                state.set('cycle', {'phase': 'implement', 'key': 'APP-1'})
            with redirect_stdout(io.StringIO()) as text:
                self.assertEqual(main(['--repo', str(root), 'status']), 0)
                self.assertIn('APP-1', text.getvalue())
            self.assertEqual(git(root, 'status', '--porcelain'), '')


class MoreEngineTests(engine_fixtures.EngineCase):
    def test_model_backlog_context_stays_bounded_as_history_grows(self):
        records = {str(n): {'recommendation': engine_fixtures.recommendation(f'Feature {n}'), 'sequence': n}
                   for n in reversed(range(1000))}
        context = self.engine.backlog_context([], records)
        self.assertEqual(len(context['recent_history']), 200)
        self.assertEqual(context['omitted_history'], 800)
        self.assertEqual(context['recent_history'][-1]['title'], 'Feature 999')

    def test_missing_validation_stops_before_jira_writes_but_review_still_works(self):
        self.cfg.commands = {'setup': [], 'checks': [], 'reason': 'No checks'}
        self.engine.review_only()
        self.agent.calls = 0
        self.assertFalse(self.jira.items)
        base = self.repo.head()
        for checkpoint in (None, {'phase': 'baseline', 'key': 'APP-1'}):
            with self.subTest(checkpoint=checkpoint):
                self.state.set('cycle', checkpoint)
                with self.assertRaisesRegex(ConfigError, 'No checks'):
                    self.engine.cycle()
                self.assertEqual(self.state.get('cycle'), checkpoint)
                self.assertFalse(self.jira.items)
                self.assertFalse(self.jira.blocked)
        self.assertEqual(self.repo.head(), base)
        self.assertEqual(self.agent.calls, 0)

    def test_failed_baseline_prevents_agent_implementation(self):
        self.cfg.commands = {'setup': [], 'checks': [
            {'name': 'fail', 'kind': 'test', 'argv': [sys.executable, '-c', 'raise SystemExit(1)']},
            {'name': 'runtime', 'kind': 'runtime', 'argv': [sys.executable, '-c', 'print(1)']}]}
        self.create_and_approve()
        with patch.object(self.agent, 'implement') as implement:
            self.engine.cycle()
            implement.assert_not_called()
        self.assertEqual(self.jira.blocked, ['APP-1'])

    def test_target_change_during_review_is_preserved_without_merge(self):
        original = self.agent.review
        def review(*args):
            (self.root / 'user.txt').write_text('user work')
            git(self.root, 'add', '.')
            git(self.root, 'commit', '-m', 'concurrent user change')
            return original(*args)
        self.agent.review = review
        self.create_and_approve()
        self.engine.cycle()
        self.assertTrue((self.root / 'user.txt').exists())
        self.assertFalse((self.root / 'APP-1.txt').exists())
        self.assertEqual(self.jira.blocked, ['APP-1'])

    def test_recovery_after_merge_before_checkpoint(self):
        self.create_and_approve()
        self.jira.fail_complete = True
        with self.assertRaises(TransientError):
            self.engine.cycle()
        cycle = self.state.get('cycle')
        cycle['phase'] = 'merging'  # Simulate crash just after Git fast-forward.
        self.state.set('cycle', cycle)
        head = self.repo.head()
        with patch.object(self.agent, 'implement') as implement:
            self.engine.cycle()
            implement.assert_not_called()
        self.assertEqual(self.repo.head(), head)

    def test_uncertain_create_not_found_never_blindly_retries(self):
        def lost(*args):
            raise TransientError('lost response')
        with patch.object(self.jira, 'create', side_effect=lost) as create:
            with self.assertRaises(TransientError):
                self.engine.cycle()
            with self.assertRaises(TransientError):
                self.engine.cycle()
            self.assertEqual(create.call_count, 1)

    def test_checking_out_verified_commit_does_not_count_as_target_merge(self):
        base = self.repo.head()
        original = self.agent.review
        def review(ticket, before, commit, *args):
            git(self.root, 'checkout', '-b', 'user-branch', commit)
            return original(ticket, before, commit, *args)
        self.agent.review = review
        self.create_and_approve()
        self.engine.cycle()
        self.assertEqual(git(self.root, 'rev-parse', 'refs/heads/main'), base)
        self.assertFalse(self.jira.completed)
        self.assertEqual(self.jira.blocked, ['APP-1'])


class MoreJiraTests(unittest.TestCase):
    def test_missing_transition_fields_are_configuration_errors(self):
        transport = FakeTransport([{'fields': {'status': {'name': 'Open'}}},
            {'transitions': [{'id': '1', 'to': {'name': 'Done'},
                              'fields': {'resolution': {'required': True}}}]}])
        with self.assertRaises(ConfigError):
            Jira(Config(), transport=transport).transition('APP-1', 'Done')

    def test_retry_after_accepts_http_dates(self):
        self.assertEqual(retry_seconds('bad'), 0)
        self.assertEqual(retry_seconds('77'), 77)
        self.assertGreater(retry_seconds('Wed, 01 Jan 2099 00:00:00 GMT'), 0)


if __name__ == '__main__':
    unittest.main()

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from improver.core import Blocked, Config, ConfigError, Runner
from improver.git import Repository
from improver.agent import Agent, validate_commands, validate_recommendations


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], stderr=subprocess.STDOUT).decode().strip()


def make_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, 'init', '-b', 'main')
    git(path, 'config', 'user.name', 'Test')
    git(path, 'config', 'user.email', 'test@example.invalid')
    (path / 'README.md').write_text('Example application', encoding='utf-8')
    git(path, 'add', '.')
    git(path, 'commit', '-m', 'initial')


class GitAgentTests(unittest.TestCase):
    def test_password_verification_source_is_visible_without_credential_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            make_repo(root)
            repo = Repository(root, Runner())
            original_key = repo.discovery_key()
            scripts = root / 'scripts'
            scripts.mkdir()
            (scripts / 'verify_password_change.py').write_text('# password change verification\n')
            (scripts / 'passwords.txt').write_text('fixture\n')
            (scripts / 'verify_password_secrets.py').write_text('fixture\n')
            (root / '.env').write_text('FIXTURE=1\n')
            git(root, 'add', '.')
            inventory = repo.context()['files']
            self.assertIn('scripts/verify_password_change.py', inventory)
            self.assertNotIn('scripts/passwords.txt', inventory)
            self.assertNotIn('scripts/verify_password_secrets.py', inventory)
            self.assertNotIn('.env', inventory)
            self.assertNotEqual(repo.discovery_key(), original_key)

    def test_worktree_merge_and_concurrent_change_guard(self):
        with tempfile.TemporaryDirectory(prefix='improver space ') as d:
            root = Path(d) / 'app'
            make_repo(root)
            repo = Repository(root, Runner())
            base = repo.head()
            work = repo.worktree('test', base)
            (work / 'feature.txt').write_text('feature')
            commit = repo.commit(work, 'APP-1', 'feature')
            repo.merge('main', base, commit)
            self.assertTrue((root / 'feature.txt').exists())
            with self.assertRaises(Blocked):
                repo.merge('main', base, commit)

    def test_dirty_repository_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            make_repo(root)
            (root / 'user.txt').write_text('uncommitted')
            with self.assertRaises(Blocked):
                Repository(root, Runner()).require_clean('main')

    def test_cleanup_removes_only_owned_clean_worktrees_including_ignored_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'app'
            make_repo(root)
            (root / '.gitignore').write_text('.venv/\n')
            git(root, 'add', '.')
            git(root, 'commit', '-m', 'ignore dependencies')
            repo = Repository(root, Runner())
            work = repo.worktree('cleanup', repo.head())
            (work / '.venv').mkdir()
            (work / '.venv' / 'generated.txt').write_text('generated')
            repo.discard_clean_worktree(work)
            self.assertFalse(work.exists())
            with self.assertRaises(ConfigError):
                repo.discard_clean_worktree(root)

    def test_private_keys_cannot_be_committed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'app'
            make_repo(root)
            repo = Repository(root, Runner())
            work = repo.worktree('secret-check', repo.head())
            (work / 'private.pem').write_text('private key')
            with self.assertRaises(Blocked):
                repo.commit(work, 'APP-1', 'must not commit secret')

    def test_malformed_agent_output_and_empty_checks_are_rejected(self):
        with self.assertRaises(Blocked):
            validate_commands({'setup': [], 'checks': []})
        with self.assertRaises(Blocked):
            validate_recommendations({'recommendations': [{'title': 'vague'}]}, 5)

    def test_fake_codex_process_contract(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fake = root / 'fake.py'
            fake.write_text('''import sys,json,pathlib
args=sys.argv
prompt=sys.stdin.read()
pathlib.Path(args[args.index('-o')+1]).write_text(json.dumps({'value': 'ok'}))
print(json.dumps({'type':'turn.completed'}))
''')
            cfg = Config(codex=[sys.executable, str(fake)])
            agent = Agent(cfg, root, Runner())
            result = agent.call('unit', 'hello', {'type':'object'}, root, False)
            self.assertEqual(result, {'value': 'ok'})


if __name__ == '__main__':
    unittest.main()

"""The controller, not the coding agent, owns commits and integration."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re

from .core import Blocked, ConfigError


EXCLUDED = {'.git', '.venv', 'venv', 'node_modules', 'vendor', 'dist', 'build',
            '__pycache__', '.next', '.cache', 'coverage', 'target'}


def readable(path):
    p = Path(path)
    name = p.name.lower()
    password_verifier = p.parent.name.lower() == 'scripts' and name.startswith('verify_password_') and p.suffix.lower() == '.py'
    return (not EXCLUDED.intersection(p.parts)
            and not name.startswith('.env')
            and not any(word in name for word in ('secret', 'credential', 'token'))
            and ('password' not in name or password_verifier)
            and p.suffix.lower() not in {'.pem', '.key', '.p12', '.pfx', '.png', '.jpg', '.pdf',
                                         '.mp4', '.zip', '.exe', '.dll', '.db', '.sqlite', '.lock'})


class Repository:
    def __init__(self, directory, runner):
        self.runner = runner
        result = runner.run(['git', 'rev-parse', '--show-toplevel'], directory)
        self.root = Path(result['stdout'].strip()).resolve()
        common = self.git('rev-parse', '--git-common-dir')
        common = Path(common)
        self.common = (self.root / common).resolve() if not common.is_absolute() else common.resolve()
        self.home = self.common / 'self-improver'
        identity = hashlib.sha256(str(self.common).casefold().encode()).hexdigest()[:8]
        # Source worktrees must not live inside .git: Codex protects Git metadata
        # from workspace writes, which can also protect descendants of that directory.
        self.worktrees = self.root.parent / f'.{self.root.name}-improver-{identity}'

    def git(self, *args, cwd=None, check=True):
        return self.runner.run(['git', '-c', 'core.quotepath=false', *args], cwd or self.root, 120, check=check)['stdout'].rstrip('\r\n')

    def head(self, cwd=None):
        return self.git('rev-parse', 'HEAD', cwd=cwd)

    def require_clean(self, branch):
        if self.git('symbolic-ref', '--short', 'HEAD') != branch:
            raise Blocked(f'Check out target branch {branch} in {self.root}.')
        if self.git('status', '--porcelain'):
            raise Blocked('Repository has user changes; commit or stash them before running.')
        if (self.common / 'MERGE_HEAD').exists() or (self.common / 'rebase-merge').exists():
            raise Blocked('Finish the current Git operation first.')
        if not self.git('config', 'user.name', check=False) or not self.git('config', 'user.email', check=False):
            raise ConfigError('Configure Git user.name and user.email before running.')

    def worktree(self, name, base):
        if not re.fullmatch(r'[a-zA-Z0-9-]+', name):
            raise ConfigError('Invalid worktree name.')
        path = self.worktrees / name
        branch = f'improver/{name}'
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if self.git('symbolic-ref', '--short', 'HEAD', cwd=path) != branch:
                raise Blocked('Worktree branch does not match checkpoint.')
            return path
        # An interrupted worktree add may have created the branch but not the directory.
        exists = self.git('show-ref', '--verify', f'refs/heads/{branch}', check=False)
        if exists:
            self.git('worktree', 'add', str(path), branch)
        else:
            self.git('worktree', 'add', '-b', branch, str(path), base)
        return path

    def discard_clean_worktree(self, path):
        path = Path(path).resolve()
        if path.parent != self.worktrees.resolve():
            raise ConfigError('Refusing to remove a worktree outside this controller\'s worktree directory.')
        branch = self.git('symbolic-ref', '--short', 'HEAD', cwd=path)
        if branch != f'improver/{path.name}':
            raise ConfigError('Refusing to remove a worktree not owned by this controller.')
        if not self.git('status', '--porcelain', cwd=path):
            self.git('worktree', 'remove', str(path))
            self.git('branch', '-d', branch)

    def commit(self, work, ticket, summary):
        self.git('add', '-A', cwd=work)
        if not self.git('diff', '--cached', '--name-only', cwd=work):
            return self.head(work)
        files = self.git('diff', '--cached', '--name-only', cwd=work).splitlines()
        for name in files:
            path = Path(work) / name
            basename = path.name.casefold()
            if (basename.startswith('.env') or basename in {'credentials.json', 'id_rsa', 'id_ed25519'}
                    or path.suffix.casefold() in {'.pem', '.key', '.p12', '.pfx'}
                    or {'node_modules', '.venv', '__pycache__'}.intersection(Path(name).parts)):
                raise Blocked(f'Refusing to commit credentials or generated dependencies: {name}')
            if path.is_symlink():
                raise Blocked(f'New or modified symlink requires manual review: {name}')
            if path.is_file() and path.stat().st_size < 2_000_000:
                data = path.read_bytes()
                if re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', data):
                    raise Blocked(f'Private key material detected in {name}.')
                if any(os.environ.get(key) and os.environ[key].encode() in data for key in ('JIRA_API_TOKEN',)):
                    raise Blocked(f'Jira credential detected in {name}.')
        self.git('commit', '-m', f'{ticket}: {summary[:180]}\n\nSelf-Improver-Ticket: {ticket}', cwd=work)
        return self.head(work)

    def merge(self, branch, base, commit):
        self.require_clean(branch)
        if self.head() != base:
            raise Blocked('Target changed during implementation; preserved worktree, no merge performed.')
        self.git('merge', '--ff-only', commit)
        if self.head() != commit or self.git('status', '--porcelain'):
            raise Blocked('A merge hook changed the verified snapshot; inspect the target before continuing.')

    def contains(self, commit, branch=None):
        target = f'refs/heads/{branch}' if branch else 'HEAD'
        result = self.runner.run(['git', 'merge-base', '--is-ancestor', commit, target], self.root, 120, check=False)
        return result['code'] == 0

    def context(self, cwd=None):
        root = Path(cwd or self.root)
        files = self.git('ls-files', '-z', '--cached', '--others', '--exclude-standard', cwd=root).split('\0')
        manifest = []
        docs = {}
        document_bytes = 0
        for name in files:
            if not name or not readable(name):
                continue
            path = root / name
            if (path.is_symlink() or not path.resolve().is_relative_to(root.resolve())
                    or not path.is_file() or path.stat().st_size > 300_000):
                continue
            try:
                data = path.read_text(encoding='utf-8')
            except (UnicodeError, OSError):
                continue
            if '\0' in data:
                continue
            manifest.append(name)
            if (document_bytes < 200_000
                    and Path(name).name.lower() in {'agents.md', 'readme.md', 'contributing.md', 'pyproject.toml', 'package.json'}):
                docs[name] = data[:12000]
                document_bytes += len(docs[name].encode('utf-8'))
        return {'commit': self.head(root), 'files': manifest[:10000], 'documents': docs,
                'omitted_inventory_paths': max(0, len(manifest) - 10000),
                'history': self.git('log', '-8', '--oneline', cwd=root)}

    def discovery_key(self):
        names = self.git('ls-files', '-z').split('\0')
        digest = hashlib.sha256()
        for name in sorted(filter(None, names)):
            p = Path(name)
            if readable(name) and (p.suffix.lower() in {'.toml', '.json', '.yaml', '.yml', '.md', '.ini', '.cfg'}
                                   or (p.suffix.lower() == '.py' and p.name.lower().startswith('verify_'))
                                   or p.name in {'Makefile', 'requirements.txt', 'gradlew', 'mvnw'}):
                path = self.root / name
                if path.is_file() and not path.is_symlink():
                    digest.update(name.encode())
                    digest.update(path.read_bytes())
        return digest.hexdigest()

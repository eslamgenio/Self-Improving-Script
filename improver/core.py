"""Configuration, durable checkpoints, locking and bounded subprocesses."""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    """An operator must fix configuration before execution can continue."""


class TransientError(RuntimeError):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


class Blocked(RuntimeError):
    """A ticket or cycle cannot safely proceed."""


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def recommendation_id(title, evidence):
    normalized = ' '.join(f'{title} {evidence}'.casefold().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:24]


@dataclass
class Config:
    app_id: str = 'application'
    target_branch: str = 'main'
    jira: dict = field(default_factory=lambda: {
        'base_url': '', 'project': '', 'board_id': 0,
        'issue_types': {'bug': '', 'improvement': '', 'feature': ''},
        'priorities': [], 'custom_fields': {},
        'statuses': {'backlog': '', 'in_progress': '', 'done': ''},
    })
    codex: list[str] = field(default_factory=lambda: ['codex'])
    model: str = ''
    skills_path: str = ''
    skills_commit: str = ''
    commands: dict | None = None
    interval: int = 300
    review_timeout: int = 1800
    implementation_timeout: int = 5400
    command_timeout: int = 1800

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ConfigError('Configuration must be a JSON object.')
        try:
            config = cls(**data)
        except TypeError as exc:
            raise ConfigError(f'Unknown configuration fields: {exc}') from exc
        forbidden = {'token', 'api_token', 'password', 'email', 'authorization'}
        if not isinstance(config.jira, dict) or forbidden & set(config.jira):
            raise ConfigError('Keep Jira credentials in JIRA_EMAIL / JIRA_API_TOKEN, not configuration.')
        priorities = config.jira.get('priorities')
        if not isinstance(priorities, list) or not priorities or any(not isinstance(p, str) or not p for p in priorities):
            raise ConfigError('jira.priorities must list Jira priority IDs from highest to lowest.')
        if len(set(priorities)) != len(priorities):
            raise ConfigError('Priority IDs must be unique.')
        import re
        if not isinstance(config.app_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,49}', config.app_id):
            raise ConfigError('app_id must be 1–50 lowercase letters, digits or hyphens.')
        if not isinstance(config.target_branch, str) or not config.target_branch or config.target_branch.startswith('-'):
            raise ConfigError('A target branch is required.')
        if not isinstance(config.codex, list) or not config.codex or any(not isinstance(s, str) or not s for s in config.codex):
            raise ConfigError('codex must be a nonempty argv array.')
        for name in ('review_timeout', 'implementation_timeout', 'command_timeout', 'interval'):
            if type(getattr(config, name)) is not int or getattr(config, name) <= 0:
                raise ConfigError(f'{name} must be a positive number of seconds.')
        for key in ('issue_types', 'statuses', 'custom_fields', 'transition_fields'):
            if key in config.jira and not isinstance(config.jira[key], dict):
                raise ConfigError(f'jira.{key} must be an object.')
        if 'board_id' in config.jira and (type(config.jira['board_id']) is not int or config.jira['board_id'] < 0):
            raise ConfigError('jira.board_id must be a nonnegative integer (set a real board ID before doctor/run).')
        for key in ('base_url', 'project'):
            if key in config.jira and not isinstance(config.jira[key], str):
                raise ConfigError(f'jira.{key} must be a string.')
        for key in ('issue_types', 'statuses'):
            if any(not isinstance(v, str) for v in config.jira.get(key, {}).values()):
                raise ConfigError(f'jira.{key} values must be strings.')
        for key in ('model', 'skills_path', 'skills_commit'):
            if not isinstance(getattr(config, key), str):
                raise ConfigError(f'{key} must be a string.')
        if config.commands is not None and not isinstance(config.commands, dict):
            raise ConfigError('commands must be an object or null for automatic discovery.')
        if any(not isinstance(fields, dict) for fields in config.jira.get('transition_fields', {}).values()):
            raise ConfigError('jira.transition_fields must map destination status names to field objects.')
        return config

    @classmethod
    def load(cls, path):
        try:
            return cls.from_dict(json.loads(Path(path).read_text(encoding='utf-8')))
        except (OSError, ValueError) as exc:
            raise ConfigError(f'Cannot load {path}: {exc}') from exc


class State:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time REAL, value TEXT);
        ''')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.set_many({key: value})

    def set_many(self, values):
        with self.db:
            self.db.executemany('INSERT OR REPLACE INTO kv VALUES (?,?)',
                                [(key, canonical(value)) for key, value in values.items()])

    def enqueue(self, key, value):
        with self.db:
            if not self.get('delivered:' + key, False):
                self.db.execute('INSERT OR IGNORE INTO outbox VALUES (?,?)', (key, canonical(value)))

    def pending(self):
        return [(key, json.loads(value)) for key, value in self.db.execute('SELECT id,value FROM outbox ORDER BY rowid')]

    def update_pending(self, key, value):
        with self.db:
            self.db.execute('UPDATE outbox SET value=? WHERE id=?', (canonical(value), key))

    def acknowledge(self, key):
        with self.db:
            self.db.execute('DELETE FROM outbox WHERE id=?', (key,))
            self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', ('delivered:' + key, 'true'))

    def event(self, **value):
        with self.db:
            self.db.execute('INSERT INTO events(time,value) VALUES (?,?)', (time.time(), canonical(value)))
            self.db.execute('DELETE FROM events WHERE id < (SELECT MAX(id)-2000 FROM events)')

    def recent(self):
        return [json.loads(r[0]) for r in self.db.execute('SELECT value FROM events ORDER BY id DESC LIMIT 10')]


class RepoLock:
    """OS-held locks release on crashes; lock-file existence is not ownership."""
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        if self.path.stat().st_size == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise ConfigError('Another improver instance is running for this repository.') from exc
        return self

    def __exit__(self, *args):
        if os.name == 'nt':
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()


def redact(text):
    for name in ('JIRA_EMAIL', 'JIRA_API_TOKEN'):
        value = os.environ.get(name)
        if value:
            text = text.replace(value, '[REDACTED]')
    import re
    return re.sub(r'(?i)(authorization\s*[:=]\s*)[^\r\n]+', r'\1[REDACTED]', text)


def configure_logging(home):
    identity = hashlib.sha256(str(Path(home).resolve()).encode()).hexdigest()[:12]
    logger = logging.getLogger('improver.' + identity)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(Path(home) / 'run.jsonl', maxBytes=2_000_000, backupCount=3, encoding='utf-8')
        logger.addHandler(handler)
    return logger


class Runner:
    def __init__(self, state=None, logger=None):
        self.state = state
        self.logger = logger

    def run(self, argv, cwd, timeout=1800, input_text=None, check=True):
        argv = list(argv)
        executable = Path(argv[0])
        if not executable.is_absolute():
            # Windows CreateProcess does not resolve executable paths relative to cwd.
            if executable.parent != Path('.'):
                argv[0] = str((Path(cwd) / executable).resolve())
            else:
                argv[0] = shutil.which(argv[0]) or argv[0]
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith('JIRA_')}
        env['GIT_TERMINAL_PROMPT'] = '0'
        env['GIT_EDITOR'] = 'true'
        kwargs = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        except OSError as exc:
            raise ConfigError(f'Cannot launch {argv[0]}: {exc}') from exc
        try:
            stdout, stderr = process.communicate(input_text.encode('utf-8') if input_text is not None else None, timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise Blocked(f'Command timed out after {timeout}s: {argv[0]}') from exc
        out = redact(stdout.decode('utf-8', errors='replace'))
        err = redact(stderr.decode('utf-8', errors='replace'))
        result = {'argv': [redact(arg) for arg in argv], 'cwd': str(cwd), 'code': process.returncode,
                  'stdout': out[-16000:], 'stderr': err[-16000:]}
        if self.state:
            self.state.event(process=result)
        if self.logger:
            self.logger.info(redact(canonical({'process': result})))
        if check and process.returncode:
            raise Blocked(f'Command failed ({process.returncode}): {argv[0]}\n{err[-3000:]}\n{out[-3000:]}')
        return {**result, 'stdout': out, 'stderr': err}

"""User-facing commands. Runtime state lives under the Git common directory."""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time

from .agent import Agent
from .core import (Blocked, Config, ConfigError, RepoLock, Runner, State, TransientError,
                   configure_logging, redact)
from .engine import Engine
from .git import Repository
from .jira import Jira


SKILLS_URL = 'https://github.com/addyosmani/agent-skills.git'


def codex_command():
    if os.name == 'nt':
        command = shutil.which('codex.cmd')
        if command:
            script = Path(command).parent / 'node_modules' / '@openai' / 'codex' / 'bin' / 'codex.js'
            if script.is_file():
                return [shutil.which('node') or 'node', str(script)]
        command = shutil.which('codex.exe')
        if command:
            return [command]
        raise ConfigError('Install Codex CLI and Node.js, or set codex to an executable argv array in config.json.')
    return [shutil.which('codex') or 'codex']


def prepare_skills(repo, runner, source, revision, *, checkout_existing):
    skills = repo.home / 'agent-skills'
    if not skills.exists():
        runner.run(['git', 'clone', source, str(skills)], repo.root, 600)
        checkout_existing = True
    if checkout_existing:
        runner.run(['git', 'checkout', '--detach', revision], skills, 120)
    commit = runner.run(['git', 'rev-parse', 'HEAD'], skills, 120)['stdout'].strip()
    return str(skills.resolve()), commit


def initialize(args, repo, runner):
    config_path = repo.home / 'config.json'
    if config_path.exists():
        raise ConfigError(f'Already initialized. Edit {config_path}; initialization never overwrites configuration.')
    branch = args.target_branch or repo.git('symbolic-ref', '--short', 'HEAD')
    repo.require_clean(branch)
    repo.home.mkdir(parents=True, exist_ok=True)
    skills_path, commit = prepare_skills(repo, runner, args.skills_source, args.skills_revision, checkout_existing=True)
    app_id = args.app_id or re.sub('[^a-z0-9]+', '-', repo.root.name.casefold()).strip('-')[:50] or 'application'
    cfg = Config(app_id=app_id, target_branch=branch, codex=codex_command(),
                 skills_path=skills_path, skills_commit=commit)
    cfg.jira.update(base_url=args.jira_url, project=args.project, board_id=args.board_id,
                    priorities=args.priority_ids.split(','))
    cfg.jira['issue_types'] = {'bug': args.bug_type, 'improvement': args.improvement_type, 'feature': args.feature_type}
    cfg.jira['statuses'] = {'backlog': args.backlog_status, 'in_progress': args.progress_status, 'done': args.done_status}
    Config.from_dict(asdict(cfg))
    # Validate the actual skill pack before writing a usable configuration.
    Agent(cfg, repo.home, runner).skills('implement')
    config_path.write_text(json.dumps(asdict(cfg), indent=2), encoding='utf-8')
    print(f'Created {config_path}\nPinned agent-skills {commit}\n'
          'Fill in Jira mappings/custom fields, export credentials, then run doctor.')


def launcher_config(path):
    try:
        settings = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        settings = {}
    except (OSError, ValueError) as exc:
        raise ConfigError(f'Cannot read launcher settings from {path}.') from exc
    if not isinstance(settings, dict):
        raise ConfigError(f'Expected a JSON object in {path}.')

    def setting(name, default):
        value = settings.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        if not isinstance(value, str):
            raise ConfigError(f'{name} must be a string in {path}.')
        return value.strip()

    jira_url = setting('jira_url', 'https://ishussien.atlassian.net').rstrip('/')
    project = setting('jira_project', 'SCRUM').upper()
    if not re.fullmatch(r'[A-Z][A-Z0-9_]*', project):
        raise ConfigError(f'jira_project must be a Jira project key in {path}.')
    board = settings.get('jira_board_id')
    board = '1' if board is None or (isinstance(board, str) and not board.strip()) else str(board).strip()
    if not re.fullmatch(r'[0-9]{1,10}', board) or not 0 < int(board) <= 2_147_483_647:
        raise ConfigError(f'jira_board_id must be a positive board ID in {path}.')
    pin = setting('skills_commit', '')
    if pin and not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', pin):
        raise ConfigError(f'skills_commit must be a Git commit hash in {path}.')

    cfg = Config(app_id='scrum', skills_commit=pin)
    cfg.jira.update(base_url=jira_url, project=project, board_id=int(board),
                    issue_types={'bug': '10003', 'improvement': '10003', 'feature': '10004'},
                    priorities=['1', '2', '3', '4', '5'],
                    statuses={'backlog': 'To Do', 'in_progress': 'In Progress', 'done': 'Done'})
    Jira(cfg).base_url()  # Validate before bootstrapping skills or connecting.
    return cfg


def parser():
    result = argparse.ArgumentParser(description='Review applications, create Jira tickets and locally integrate verified improvements.')
    result.add_argument('--repo', default='.', help='Application repository directory (default: current directory).')
    result.add_argument('--launcher-settings', type=Path, help='Use setup-scrum.json for the Jira target.')
    commands = result.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='Create config and pin agent-skills; no Jira tickets or source changes.')
    init.add_argument('--app-id')
    init.add_argument('--target-branch')
    init.add_argument('--jira-url', default='https://YOUR-SITE.atlassian.net')
    init.add_argument('--project', default='APP')
    init.add_argument('--board-id', type=int, default=0)
    init.add_argument('--priority-ids', default='1,2,3,4,5', help='Actual Jira priority IDs, highest first; replace instance-specific defaults.')
    init.add_argument('--bug-type', default='')
    init.add_argument('--improvement-type', default='')
    init.add_argument('--feature-type', default='')
    init.add_argument('--backlog-status', default='To Do')
    init.add_argument('--progress-status', default='In Progress')
    init.add_argument('--done-status', default='Done')
    init.add_argument('--skills-source', default=SKILLS_URL, help='Official repository or an audited local clone.')
    init.add_argument('--skills-revision', default='HEAD', help='Resolve once and pin this Git revision.')
    commands.add_parser('doctor', help='Check configuration, authentication and baseline validation.')
    run = commands.add_parser('run', help='Run until interrupted, resuming incomplete cycles.')
    run.add_argument('--once', action='store_true')
    commands.add_parser('status', help='Read current state and recent events without contacting Jira/Codex.')
    commands.add_parser('review', help='Read-only application review; no Jira writes or validation commands.')
    resolve = commands.add_parser('resolve-create', help='Resolve an ambiguous Jira creation after manually checking Jira.')
    resolve.add_argument('fingerprint')
    choice = resolve.add_mutually_exclusive_group(required=True)
    choice.add_argument('--issue', help='The Jira key that was created; identity labels will be verified.')
    choice.add_argument('--not-created', action='store_true', help='Assert Jira did not create the ticket and allow a retry.')
    resolve_comment = commands.add_parser('resolve-comment', help='Retry a comment after checking it was not posted in Jira.')
    resolve_comment.add_argument('operation_id')
    resolve_comment.add_argument('--not-posted', action='store_true', required=True)
    return result


def doctor(config, repo, state, jira, agent, runner):
    repo.require_clean(config.target_branch)
    agent.skills('implement')
    runner.run([*config.codex, '--version'], repo.root, 30)
    runner.run([*config.codex, 'login', 'status'], repo.root, 30)
    jira_result = jira.doctor()
    engine = Engine(config, repo, state, jira, agent, runner)
    checks = engine.baseline_commands()
    import uuid
    work = repo.worktree(f'doctor-{uuid.uuid4().hex[:8]}', repo.head())
    engine.run_checks(checks, work, setup=True)
    if repo.git('diff', 'HEAD', '--', cwd=work):
        raise Blocked('Baseline validation modified tracked source.')
    repo.discard_clean_worktree(work)
    return {**jira_result, 'git': 'OK', 'codex': 'OK', 'skills': config.skills_commit, 'validation': checks}


def execute(args):
    runner = Runner()
    repo = Repository(Path(args.repo).resolve(), runner)
    config = launcher_config(args.launcher_settings) if args.launcher_settings is not None else None
    home = repo.home
    if config is not None:
        target = json.dumps([config.jira[key] for key in ('base_url', 'project', 'board_id')])
        home = repo.home / 'launcher' / hashlib.sha256(target.encode()).hexdigest()[:24]
    if args.command == 'status':
        database = home / 'state.db'
        if not database.exists():
            print('No cycles recorded. Run init, doctor, then run.')
            return 0
        # Read-only SQLite connection: status does not create or upgrade state.
        import sqlite3
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            values = {k: json.loads(v) for k, v in db.execute('SELECT key,value FROM kv')}
            recent = [json.loads(v) for (v,) in db.execute('SELECT value FROM events ORDER BY id DESC LIMIT 10')]
            pending = [{'operation_id': key, **json.loads(value)}
                       for key, value in db.execute('SELECT id,value FROM outbox')]
        uncertain = [fp for fp, record in values.get('recommendations', {}).items()
                     if record.get('attempted') and not record.get('key')]
        print(redact(json.dumps({'cycle': values.get('cycle'), 'last_cycle': values.get('last_cycle'),
                                 'uncertain_creations': uncertain, 'pending_jira_updates': pending,
                                 'recent': recent}, indent=2)))
        return 0
    with RepoLock(repo.home / 'instance.lock'):
        if args.command == 'init':
            if config is not None:
                raise ConfigError('Launcher settings are initialized by doctor.')
            initialize(args, repo, runner)
            return 0
        if config is None:
            config = Config.load(repo.home / 'config.json')
        elif args.command in {'doctor', 'run', 'review'}:
            config.codex = codex_command()
            config.skills_path, commit = prepare_skills(repo, runner, SKILLS_URL, config.skills_commit or 'HEAD',
                                                        checkout_existing=False)
            config.skills_commit = config.skills_commit or commit
        with State(home / 'state.db') as state:
            logger = configure_logging(home)
            runner = Runner(state, logger)
            repo.runner = runner
            jira, agent = Jira(config), Agent(config, home, runner)
            engine = Engine(config, repo, state, jira, agent, runner)
            if args.command == 'doctor':
                result = doctor(config, repo, state, jira, agent, runner)
                if args.launcher_settings is not None:
                    result['launcher_settings'] = dict(jira_url=config.jira['base_url'], jira_project=config.jira['project'],
                                                       jira_board_id=config.jira['board_id'], skills_commit=config.skills_commit)
                print(json.dumps(result, indent=2))
            elif args.command == 'review':
                print(json.dumps(engine.review_only(), indent=2))
            elif args.command == 'resolve-create':
                records = state.get('recommendations', {})
                record = records.get(args.fingerprint)
                if not record or record.get('key') or not record.get('attempted'):
                    raise ConfigError('Fingerprint is not an unresolved attempted creation.')
                if args.issue:
                    issue = jira.issue(args.issue)
                    labels = issue['fields'].get('labels', [])
                    if not {jira.label, 'self-improver', f'self-improver-fp-{args.fingerprint}'} <= set(labels):
                        raise ConfigError('Issue identity labels do not match this recommendation/application.')
                    record['key'] = args.issue
                else:
                    matches = jira.issues(f'labels = "self-improver-fp-{args.fingerprint}"')
                    if matches:
                        raise ConfigError('Matching tickets exist; use --issue instead.')
                    record['attempted'] = False
                updates = {'recommendations': records}
                if args.issue:
                    updates['recent_jira_create'] = args.issue
                state.set_many(updates)
                print('Creation resolved. The next run resumes the interrupted cycle.')
            elif args.command == 'resolve-comment':
                item = dict(state.pending()).get(args.operation_id)
                if not item or not item.get('comment_attempted') or item.get('comment_delivered'):
                    raise ConfigError('Operation ID is not an unresolved attempted Jira comment.')
                item['comment_attempted'] = False
                state.update_pending(args.operation_id, item)
                print('Comment resolved. The next run will check Jira before posting.')
            elif args.command == 'run':
                # Static preflight once, including Jira field/workflow mappings. Baseline runs per ticket.
                agent.skills('implement')
                runner.run([*config.codex, 'login', 'status'], repo.root, 30)
                validated = False
                failures = 0
                while True:
                    try:
                        if not validated:
                            jira.doctor()
                            validated = True
                        engine.cycle()
                        failures = 0
                        if args.once:
                            return 1 if state.get('last_cycle', {}).get('outcome') == 'blocked' else 0
                        delay = config.interval
                    except TransientError as exc:
                        failures += 1
                        delay = max(min(30 * 2 ** min(failures - 1, 10), 1800), exc.retry_after)
                        state.event(error=redact(str(exc)), retry_in=delay)
                        print(redact(f'Transient failure: {exc}; retry in {delay}s.'), flush=True)
                        if args.once:
                            return 1
                    except Blocked as exc:
                        state.event(blocked=redact(str(exc)))
                        print(redact(f'Cycle blocked: {exc}'), flush=True)
                        if args.once:
                            return 1
                        delay = config.interval
                    # Short chunks keep shutdown responsive even with long server Retry-After values.
                    deadline = time.monotonic() + delay
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        time.sleep(min(1, remaining))
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return execute(args)
    except KeyboardInterrupt:
        print('\nStopped. Checkpoint and unfinished worktrees preserved; run again to resume.')
        return 130
    except (ConfigError, Blocked, TransientError) as exc:
        print(redact(f'Error: {exc}'))
        return 2

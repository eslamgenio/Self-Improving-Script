"""User-facing commands. Runtime state lives under the Git common directory."""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict
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


def initialize(args, repo, runner):
    config_path = repo.home / 'config.json'
    if config_path.exists():
        raise ConfigError(f'Already initialized. Edit {config_path}; initialization never overwrites configuration.')
    branch = args.target_branch or repo.git('symbolic-ref', '--short', 'HEAD')
    repo.require_clean(branch)
    repo.home.mkdir(parents=True, exist_ok=True)
    skills = repo.home / 'agent-skills'
    if not skills.exists():
        runner.run(['git', 'clone', args.skills_source, str(skills)], repo.root, 600)
    runner.run(['git', 'checkout', '--detach', args.skills_revision], skills, 120)
    commit = runner.run(['git', 'rev-parse', 'HEAD'], skills, 120)['stdout'].strip()
    app_id = args.app_id or re.sub('[^a-z0-9]+', '-', repo.root.name.casefold()).strip('-')[:50] or 'application'
    cfg = Config(app_id=app_id, target_branch=branch, codex=codex_command(),
                 skills_path=str(skills.resolve()), skills_commit=commit)
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


def parser():
    result = argparse.ArgumentParser(description='Review applications, create Jira tickets and locally integrate verified improvements.')
    result.add_argument('--repo', default='.', help='Application repository directory (default: current directory).')
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
    print(json.dumps({**jira_result, 'git': 'OK', 'codex': 'OK', 'skills': config.skills_commit,
                      'validation': checks}, indent=2))


def execute(args):
    runner = Runner()
    repo = Repository(Path(args.repo).resolve(), runner)
    if args.command == 'status':
        database = repo.home / 'state.db'
        if not database.exists():
            print('No cycles recorded. Run init, doctor, then run.')
            return 0
        # Read-only SQLite connection: status does not create or upgrade state.
        import sqlite3
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            values = {k: json.loads(v) for k, v in db.execute('SELECT key,value FROM kv')}
            recent = [json.loads(v) for (v,) in db.execute('SELECT value FROM events ORDER BY id DESC LIMIT 10')]
            pending = [json.loads(v) for (v,) in db.execute('SELECT value FROM outbox')]
        uncertain = [fp for fp, record in values.get('recommendations', {}).items()
                     if record.get('attempted') and not record.get('key')]
        print(redact(json.dumps({'cycle': values.get('cycle'), 'last_cycle': values.get('last_cycle'),
                                 'uncertain_creations': uncertain, 'pending_jira_updates': pending,
                                 'recent': recent}, indent=2)))
        return 0
    with RepoLock(repo.home / 'instance.lock'):
        if args.command == 'init':
            initialize(args, repo, runner)
            return 0
        config = Config.load(repo.home / 'config.json')
        with State(repo.home / 'state.db') as state:
            logger = configure_logging(repo.home)
            runner = Runner(state, logger)
            repo.runner = runner
            jira, agent = Jira(config), Agent(config, repo.home, runner)
            engine = Engine(config, repo, state, jira, agent, runner)
            if args.command == 'doctor':
                doctor(config, repo, state, jira, agent, runner)
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
                state.set('recommendations', records)
                print('Creation resolved. The next run resumes the interrupted cycle.')
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

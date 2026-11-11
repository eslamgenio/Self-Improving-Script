"""User-facing commands. Runtime state lives under the Git common directory."""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict
from getpass import GetPassWarning, getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from tempfile import NamedTemporaryFile
import time
import warnings

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
    settings = collect_jira_settings(config_path, {
        'jira_url': args.jira_url, 'jira_project': args.project, 'jira_board_id': args.board_id,
        'jira': {
            'issue_types': {'bug': args.bug_type, 'improvement': args.improvement_type, 'feature': args.feature_type},
            'statuses': {'backlog': args.backlog_status, 'in_progress': args.progress_status, 'done': args.done_status},
            'priorities': parse_priority_ids(args.priority_ids) if not missing_setting(args.priority_ids) else None,
        },
    })
    jira = launcher_config(config_path, settings).jira
    branch = args.target_branch or repo.git('symbolic-ref', '--short', 'HEAD')
    repo.require_clean(branch)
    repo.home.mkdir(parents=True, exist_ok=True)
    skills_path, commit = prepare_skills(repo, runner, args.skills_source or SKILLS_URL,
                                        args.skills_revision or 'HEAD', checkout_existing=True)
    app_id = args.app_id or re.sub('[^a-z0-9]+', '-', repo.root.name.casefold()).strip('-')[:50] or 'application'
    cfg = Config(app_id=app_id, target_branch=branch, codex=codex_command(),
                 skills_path=skills_path, skills_commit=commit)
    cfg.jira.update(jira)
    Config.from_dict(asdict(cfg))
    # Validate the actual skill pack before writing a usable configuration.
    Agent(cfg, repo.home, runner).skills('implement')
    config_path.write_text(json.dumps(asdict(cfg), indent=2), encoding='utf-8')
    print(f'Created {config_path}\nPinned agent-skills {commit}\n'
          'Configure any required Jira custom fields, export credentials, then run doctor.')


def read_launcher_settings(path):
    try:
        contents = path.read_text(encoding='utf-8-sig')
        settings = json.loads(contents) if contents.strip() else {}
    except FileNotFoundError:
        settings = {}
    except (OSError, ValueError) as exc:
        raise ConfigError(f'Cannot read launcher settings from {path}.') from exc
    if not isinstance(settings, dict):
        raise ConfigError(f'Expected a JSON object in {path}.')
    return settings


def prompt_setting(path, name, label, read):
    if not sys.stdin.isatty():
        raise ConfigError(f'Set {name} in {path} or run interactively.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', GetPassWarning)
            while True:
                value = read(f'{label}: ').strip()
                if value:
                    return value
    except (EOFError, GetPassWarning) as exc:
        raise ConfigError(f'Could not read {name} interactively. Set it in {path}.') from exc


def missing_setting(value):
    return value is None or (isinstance(value, str) and not value.strip())


def parse_priority_ids(value):
    return [priority.strip() for priority in value.split(',')]


def collect_jira_settings(path, settings):
    completed = settings.copy()

    def fill(values, fields, prefix=''):
        for name, label in fields.items():
            if missing_setting(values.get(name)):
                values[name] = prompt_setting(path, prefix + name, label, input)

    fill(completed, {'jira_url': 'Jira base URL', 'jira_project': 'Jira project key', 'jira_board_id': 'Jira board ID'})
    jira = completed.get('jira', {})
    if not isinstance(jira, dict):
        raise ConfigError(f'jira must be a JSON object in {path}.')
    jira = jira.copy()
    for name, fields in (
        ('issue_types', {'bug': 'Bug issue type ID', 'improvement': 'Improvement issue type ID',
                         'feature': 'Feature issue type ID'}),
        ('statuses', {'backlog': 'Backlog status name', 'in_progress': 'In-progress status name',
                      'done': 'Completed status name'}),
    ):
        values = jira.get(name, {})
        if not isinstance(values, dict):
            raise ConfigError(f'jira.{name} must be a JSON object in {path}.')
        jira[name] = values.copy()
        fill(jira[name], fields, prefix=f'jira.{name}.')
    if missing_setting(jira.get('priorities')) or jira['priorities'] == []:
        priorities = prompt_setting(path, 'jira.priorities', 'Jira priority IDs, highest first, separated by commas', input)
        jira['priorities'] = parse_priority_ids(priorities)
    completed['jira'] = jira
    return completed


def collect_launcher_settings(path, settings, repo_override):
    completed = settings.copy()
    if missing_setting(completed.get('application_path')):
        if repo_override is None:
            application = prompt_setting(path, 'application_path', 'Application repository path', input)
            application = application.strip('"').strip()
            if not application:
                raise ConfigError(f'Set application_path in {path} or pass --repo.')
            completed['application_path'] = str(Path(application).expanduser().resolve())
        else:
            completed['application_path'] = str(Path(repo_override).expanduser().resolve())
    completed = collect_jira_settings(path, completed)
    for name, label, read in (
        ('jira_email', 'Atlassian sign-in email', input),
        ('jira_api_token', 'Jira API token', getpass),
    ):
        value = completed.get(name)
        if missing_setting(value):
            value = prompt_setting(path, name, label, read)
        if not isinstance(value, str):
            raise ConfigError(f'{name} must be a string in {path}.')
        completed[name] = value.strip()
    return completed


def launcher_repo_path(path, settings):
    application = settings.get('application_path')
    if not isinstance(application, str) or not application.strip():
        raise ConfigError(f'Set application_path in {path} or pass --repo.')
    return path.parent / Path(application.strip()).expanduser()


def save_launcher_settings(path, original, updated):
    if original == updated:
        return
    if read_launcher_settings(path) != original:
        raise ConfigError(f'{path} changed during validation. Review it and run again.')
    temp_path = None
    try:
        with NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, prefix='setup-scrum-',
                                suffix='.tmp', delete=False) as temp:
            temp_path = Path(temp.name)
            json.dump(updated, temp, indent=2, ensure_ascii=False)
            temp.write('\n')
        temp_path.replace(path)
    except OSError as exc:
        raise ConfigError(f'Could not save launcher settings to {path}.') from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def launcher_config(path, settings):
    def required_text(name):
        value = settings.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f'Set {name} in {path} or run doctor interactively.')
        return value.strip()

    jira_url = required_text('jira_url').rstrip('/')
    project = required_text('jira_project').upper()
    if not re.fullmatch(r'[A-Z][A-Z0-9_]*', project):
        raise ConfigError(f'jira_project must be a Jira project key in {path}.')
    board = str(settings.get('jira_board_id', '')).strip()
    if not re.fullmatch(r'[0-9]{1,10}', board) or not 0 < int(board) <= 2_147_483_647:
        raise ConfigError(f'jira_board_id must be a positive board ID in {path}.')
    pin = settings.get('skills_commit')
    if not missing_setting(pin) and (not isinstance(pin, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', pin.strip())):
        raise ConfigError(f'skills_commit must be a Git commit hash in {path}.')

    jira = settings.get('jira')
    if not isinstance(jira, dict):
        raise ConfigError(f'Set the Jira mappings in the jira object in {path} or run doctor interactively.')
    cfg = Config.from_dict({'app_id': 'scrum', 'skills_commit': pin.strip() if not missing_setting(pin) else '',
                            'jira': {**jira, 'base_url': jira_url, 'project': project, 'board_id': int(board)}})
    Jira(cfg).base_url()  # Validate before bootstrapping skills or connecting.
    return cfg


def parser():
    result = argparse.ArgumentParser(description='Review applications, create Jira tickets and locally integrate verified improvements.')
    result.add_argument('--repo', help='Application repository directory (overrides application_path).')
    settings_mode = result.add_mutually_exclusive_group()
    settings_mode.add_argument('--launcher-settings', type=Path, help='Use a specific setup-scrum.json file.')
    settings_mode.add_argument('--repo-settings', action='store_true', help='Use the application repository configuration.')
    commands = result.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='Save launcher settings, or create a config and pin skills in repository mode.',
                               description='Set up launcher settings. In repository mode, use the options below '
                                           'or answer the prompts for missing Jira settings.')
    init.add_argument('--app-id')
    init.add_argument('--target-branch')
    init.add_argument('--jira-url')
    init.add_argument('--project')
    init.add_argument('--board-id', type=int)
    init.add_argument('--priority-ids', help='Comma-separated Jira priority IDs, highest first.')
    init.add_argument('--bug-type')
    init.add_argument('--improvement-type')
    init.add_argument('--feature-type')
    init.add_argument('--backlog-status')
    init.add_argument('--progress-status')
    init.add_argument('--done-status')
    init.add_argument('--skills-source', help='Official repository or an audited local clone.')
    init.add_argument('--skills-revision', help='Resolve once and pin this Git revision.')
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


def execute(args, settings, original_settings):
    runner = Runner()
    repo_path = Path(args.repo or '.')
    config = None
    if args.launcher_settings is not None:
        config = launcher_config(args.launcher_settings, settings)
        if args.repo is None:
            repo_path = launcher_repo_path(args.launcher_settings, settings)
    repo = Repository(repo_path.resolve(), runner)
    home = repo.home
    if config is not None:
        target = json.dumps([config.jira[key] for key in ('base_url', 'project', 'board_id')])
        home = repo.home / 'launcher' / hashlib.sha256(target.encode()).hexdigest()[:24]
    if args.command == 'status':
        database = home / 'state.db'
        if not database.exists():
            print('No cycles recorded. Run doctor, then run.' if config is not None else
                  'No cycles recorded. Run init, doctor, then run.')
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
            initialize(args, repo, runner)
            return 0
        if config is None:
            config = Config.load(repo.home / 'config.json')
        elif args.command in {'doctor', 'run', 'review'}:
            config.codex = codex_command()
            config.skills_path, commit = prepare_skills(repo, runner, SKILLS_URL, config.skills_commit or 'HEAD',
                                                        checkout_existing=False)
            config.skills_commit = config.skills_commit or commit
        if args.launcher_settings is not None and args.command in {'doctor', 'run'}:
            resolved = dict(jira_url=config.jira['base_url'], jira_project=config.jira['project'],
                            jira_board_id=config.jira['board_id'], skills_commit=config.skills_commit)
            application = settings.get('application_path')
            if not isinstance(application, str) or not application.strip():
                application = str(repo.root)
            updated_settings = {**settings, **resolved, 'application_path': application,
                                'jira_email': os.environ['JIRA_EMAIL'], 'jira_api_token': os.environ['JIRA_API_TOKEN']}
        with State(home / 'state.db') as state:
            logger = configure_logging(home)
            runner = Runner(state, logger)
            repo.runner = runner
            jira, agent = Jira(config), Agent(config, home, runner)
            engine = Engine(config, repo, state, jira, agent, runner)
            if args.command == 'doctor':
                result = doctor(config, repo, state, jira, agent, runner)
                if args.launcher_settings is not None:
                    result['launcher_settings'] = resolved
                    save_launcher_settings(args.launcher_settings, original_settings, updated_settings)
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
                    updates['recent_jira_create'] = list(dict.fromkeys([*engine.recent_jira_creates(), args.issue]))
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
                validated = args.launcher_settings is not None and updated_settings != original_settings
                if validated:
                    doctor(config, repo, state, jira, agent, runner)
                    save_launcher_settings(args.launcher_settings, original_settings, updated_settings)
                    print(f'Saved launcher settings to {args.launcher_settings}.', flush=True)
                else:
                    agent.skills('implement')
                    runner.run([*config.codex, 'login', 'status'], repo.root, 30)
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


def main(argv=None, *, default_settings=None):
    args = parser().parse_args(argv)
    previous_credentials = None
    try:
        if args.launcher_settings is None and not args.repo_settings:
            args.launcher_settings = default_settings
        original_settings = settings = {}
        if args.launcher_settings is not None:
            original_settings = settings = read_launcher_settings(args.launcher_settings)
            if args.command == 'init':
                repo_options = ('app_id', 'target_branch', 'jira_url', 'project', 'board_id', 'priority_ids',
                                'bug_type', 'improvement_type', 'feature_type', 'backlog_status', 'progress_status',
                                'done_status', 'skills_source', 'skills_revision')
                if any(getattr(args, option) is not None for option in repo_options):
                    raise ConfigError('Use --repo-settings before init to pass repository initialization options.')
            if args.command in {'init', 'doctor', 'run'}:
                settings = collect_launcher_settings(args.launcher_settings, settings, args.repo)
            if args.command == 'init':
                config = launcher_config(args.launcher_settings, settings)
                repo_path = (Path(args.repo).expanduser() if args.repo is not None
                             else launcher_repo_path(args.launcher_settings, settings))
                if not repo_path.is_dir():
                    raise ConfigError('application_path must point to an existing directory.')
                updated_settings = {**settings, 'jira_url': config.jira['base_url'],
                                    'jira_project': config.jira['project'], 'jira_board_id': config.jira['board_id']}
                if args.repo is not None:
                    updated_settings['application_path'] = str(repo_path.resolve())
                save_launcher_settings(args.launcher_settings, original_settings, updated_settings)
                print(f'Launcher settings are ready in {args.launcher_settings}.')
                return 0
            previous_credentials = {name: os.environ.get(name) for name in ('JIRA_EMAIL', 'JIRA_API_TOKEN')}
            email = settings.get('jira_email')
            token = settings.get('jira_api_token')
            email = email.strip() if isinstance(email, str) else ''
            token = token.strip() if isinstance(token, str) else ''
            os.environ['JIRA_EMAIL'] = email
            os.environ['JIRA_API_TOKEN'] = token
        return execute(args, settings, original_settings)
    except KeyboardInterrupt:
        print('\nStopped. Checkpoint and unfinished worktrees preserved; run again to resume.')
        return 130
    except (ConfigError, Blocked, TransientError) as exc:
        print(redact(f'Error: {exc}'))
        return 2
    finally:
        if previous_credentials is not None:
            for name, value in previous_credentials.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

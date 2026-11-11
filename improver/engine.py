"""Checkpointed review → Jira → worktree → verify → integrate state machine."""
from __future__ import annotations

import re
import uuid

from .agent import validate_commands, validate_recommendations, validate_schema, IMPLEMENT_SCHEMA, REVIEW_SCHEMA
from .core import Blocked, ConfigError, TransientError, canonical, recommendation_id, redact


class Engine:
    def __init__(self, config, repo, state, jira, agent, runner):
        self.config, self.repo, self.state = config, repo, state
        self.jira, self.agent, self.runner = jira, agent, runner

    def checkpoint(self, cycle, phase, **changes):
        cycle.update(phase=phase, **changes)
        self.state.set('cycle', cycle)
        self.state.event(phase=phase, ticket=cycle.get('key'))
        print(f'[{phase}] {cycle.get("key", "")}', flush=True)

    def commands(self):
        if self.config.commands is not None:
            return self.config.commands
        key = self.repo.discovery_key()
        cache = self.state.get('commands', {})
        if cache.get('key') != key:
            commands = self.agent.discover(self.repo.context(), self.repo.root)
            cache = {'key': key, 'commands': commands}
            self.state.set('commands', cache)
        return cache['commands']

    def baseline_commands(self):
        commands = None
        try:
            commands = self.commands()
            return validate_commands(commands)
        except Blocked as exc:
            message = f'Baseline validation unavailable. {exc}'
            if isinstance(commands, dict) and commands.get('reason'):
                message += f'\n{commands["reason"]}'
            self.state.event(blocked=redact(message))
            raise ConfigError(message) from exc

    def review_only(self):
        records = self.state.get('recommendations', {})
        context = self.repo.context()
        response = self.agent.recommend(context, self.backlog_context([], records), self.config.jira['priorities'], self.repo.root)
        recommendations = validate_recommendations(response, len(self.config.jira['priorities']))
        # No Jira calls, source modifications, commits, or command execution.
        return {'commit': context['commit'], 'recommendations': recommendations, 'notes': response.get('notes', '')}

    def backlog_context(self, issues, records):
        """Bound model context as the persistent backlog grows over long runs."""
        priorities = self.config.jira['priorities']
        active = [i for i in issues if i['fields'].get('status', {}).get('statusCategory', {}).get('key') != 'done']
        def order(issue):
            fields = issue['fields']
            priority = (fields.get('priority') or {}).get('id')
            return (priorities.index(priority) if priority in priorities else len(priorities),
                    fields.get('created', ''), issue['key'])
        ordered = sorted(active, key=order)
        tickets = [{'key': i['key'], 'title': i['fields'].get('summary', '')[:500],
                    'priority': i['fields'].get('priority'), 'labels': i['fields'].get('labels', []),
                    'description_excerpt': canonical(i['fields'].get('description', ''))[:2000]}
                   for i in ordered[:200]]
        recent = sorted(records.values(), key=lambda r: r.get('sequence', 0))[-200:]
        history = [{'title': r['recommendation']['title'][:500],
                    'evidence': r['recommendation']['evidence'][:2], 'key': r.get('key'),
                    'outcome': r.get('outcome', 'pending')} for r in recent]
        return {'open_tickets': tickets, 'recent_history': history,
                'omitted_open_tickets': max(0, len(active) - len(tickets)),
                'omitted_history': max(0, len(records) - len(history)),
                'note': 'Controller still checks exact titles/fingerprints and selects priorities across the full backlog.'}

    def flush(self):
        for identity, item in self.state.pending():
            if item['kind'] == 'backlog':
                self.jira.place_backlog(item['key'])
            elif item['kind'] == 'done':
                self.jira.complete(item['key'], item['result'])
            elif item['kind'] == 'blocked':
                self.jira.block(item['key'], item['reason'])
            else:
                raise ConfigError(f'Unknown outbox operation {item["kind"]}.')
            self.state.acknowledge(identity)

    def publish(self, cycle):
        records = self.state.get('recommendations', {})
        for fingerprint in cycle['fingerprints']:
            record = records[fingerprint]
            if not record.get('key'):
                if record.get('attempted'):
                    matches = self.jira.issues(f'labels = "self-improver-fp-{fingerprint}"')
                    if len(matches) != 1:
                        raise TransientError(
                            f'Uncertain Jira creation {fingerprint}: found {len(matches)} issues. '
                            'No create retry will be made. Check Jira, then use resolve-create.', 60)
                    record['key'] = matches[0]['key']
                else:
                    record['attempted'] = True
                    self.state.set('recommendations', records)
                    try:
                        record['key'] = self.jira.create(record['recommendation'], fingerprint, record['base'])
                    except ConfigError:
                        # Jira rejected this request definitively (4xx or invalid local config).
                        record['attempted'] = False
                        self.state.set('recommendations', records)
                        raise
                self.state.set('recommendations', records)
            self.state.enqueue(f'backlog:{record["key"]}', {'kind': 'backlog', 'key': record['key']})
        self.flush()

    def new_cycle(self):
        context = self.repo.context()
        backlog = self.jira.issues()
        records = self.state.get('recommendations', {})
        result = self.agent.recommend(context, self.backlog_context(backlog, records),
                                      self.config.jira['priorities'], self.repo.root)
        recs = validate_recommendations(result, len(self.config.jira['priorities']))
        titles = {' '.join(r['recommendation']['title'].casefold().split()) for r in records.values()}
        titles.update(' '.join(i['fields']['summary'].casefold().split()) for i in backlog)
        fingerprints = []
        for rec in recs:
            title = ' '.join(rec['title'].casefold().split())
            # Line-number movement must not make an otherwise identical recommendation new.
            evidence_tokens = re.sub(r':\d+(?:[-:]\d+)?', '', rec['evidence'][0]).split()
            evidence = evidence_tokens[0] if evidence_tokens else ''
            fingerprint = recommendation_id(title, evidence)
            if title in titles or fingerprint in records:
                continue
            # Require evidence grounded in at least one inventoried source path.
            if not any(any(path in entry for path in context['files']) for entry in rec['evidence']):
                raise Blocked(f'Recommendation has no source evidence: {rec["title"]}')
            titles.add(title)
            records[fingerprint] = {'recommendation': rec, 'base': context['commit'],
                                    'attempted': False, 'sequence': len(records)}
            fingerprints.append(fingerprint)
        cycle = {'id': uuid.uuid4().hex[:12], 'base': context['commit'], 'fingerprints': fingerprints}
        # Recommendations and the publish checkpoint must become durable together.
        cycle['phase'] = 'publish'
        self.state.set_many({'recommendations': records, 'cycle': cycle})
        self.checkpoint(cycle, 'publish')
        return cycle

    def run_checks(self, commands, cwd, setup=False):
        evidence = []
        if setup:
            for argv in commands['setup']:
                evidence.append(self.runner.run(argv, cwd, self.config.command_timeout))
        for check in commands['checks']:
            result = self.runner.run(check['argv'], cwd, self.config.command_timeout)
            evidence.append({'name': check['name'], 'kind': check['kind'], **result})
        return evidence

    def block(self, cycle, reason):
        self.checkpoint(cycle, 'blocked', reason=str(reason))
        self.finish_block(cycle)

    def finish_block(self, cycle):
        self.state.enqueue(f'blocked:{cycle["key"]}:{cycle["id"]}',
                           {'kind': 'blocked', 'key': cycle['key'], 'reason': cycle['reason']})
        self.flush()
        self.finish(cycle, 'blocked')

    def finish(self, cycle, outcome):
        self.state.event(outcome=outcome, ticket=cycle.get('key'), commit=cycle.get('commit'), reason=cycle.get('reason'))
        records = self.state.get('recommendations', {})
        for record in records.values():
            if record.get('key') == cycle.get('key') and cycle.get('key'):
                record.update(outcome=outcome, reason=cycle.get('reason'), commit=cycle.get('commit'))
        self.state.set_many({'last_cycle': {**cycle, 'outcome': outcome}, 'cycle': None, 'recommendations': records})

    def cycle(self):
        cycle = self.state.get('cycle')
        if cycle is None:
            self.repo.require_clean(self.config.target_branch)
        commands = None
        if cycle is None or cycle['phase'] in {'publish', 'select', 'baseline'}:
            commands = self.baseline_commands()
        self.flush()
        if cycle is None:
            cycle = self.new_cycle()
        if cycle['phase'] == 'publish':
            self.publish(cycle)
            self.checkpoint(cycle, 'select')
        if cycle['phase'] == 'select':
            selected = self.jira.select(self.jira.issues())
            if not selected:
                self.finish(cycle, 'no eligible work')
                return
            self.checkpoint(cycle, 'baseline', key=selected['key'], ticket=selected,
                            work_name=f'ticket-{selected["key"]}-{cycle["id"]}', attempts=0)
        if cycle['phase'] == 'blocked':
            self.finish_block(cycle)
            return
        if cycle['phase'] == 'baseline':
            try:
                self.repo.require_clean(self.config.target_branch)
                if self.repo.head() != cycle['base']:
                    raise Blocked('Target changed after review; rerun on its latest code.')
                self.checkpoint(cycle, 'baseline', commands=commands)
                baseline = self.repo.worktree(f'baseline-{cycle["id"]}', cycle['base'])
                evidence = self.run_checks(commands, baseline, setup=True)
                if self.repo.head(baseline) != cycle['base'] or self.repo.git('diff', 'HEAD', '--', cwd=baseline):
                    raise Blocked('Baseline setup/validation modified tracked source.')
                self.repo.discard_clean_worktree(baseline)
                work = self.repo.worktree(cycle['work_name'], cycle['base'])
                self.jira.transition(cycle['key'], self.config.jira['statuses']['in_progress'])
                self.checkpoint(cycle, 'implement', baseline_evidence=evidence, work=str(work), feedback='')
            except Blocked as exc:
                self.block(cycle, exc)
                return
        if cycle['phase'] == 'implement':
            self.implement(cycle)
        if cycle['phase'] == 'merging':
            # A crash after Git integration must not cause a second implementation.
            if not self.repo.contains(cycle['commit'], self.config.target_branch):
                try:
                    latest = self.jira.issue(cycle['key'])['fields']
                    if 'self-improver-blocked' in latest.get('labels', []) or latest['status'].get('statusCategory', {}).get('key') == 'done':
                        raise Blocked('Ticket was blocked or completed in Jira before merge.')
                    self.repo.merge(self.config.target_branch, cycle['base'], cycle['commit'])
                except Blocked as exc:
                    self.block(cycle, exc)
                    return
            self.checkpoint(cycle, 'merged')
        if cycle['phase'] == 'merged':
            self.state.enqueue(f'done:{cycle["key"]}:{cycle["id"]}', {
                'kind': 'done', 'key': cycle['key'],
                'result': {'ticket': cycle['key'], 'base': cycle['base'], 'commit': cycle['commit'],
                           'summary': cycle['implementation']['summary'], 'verified': True,
                           'acceptance_evidence': cycle['implementation']['acceptance_evidence'],
                           'checks': [{'name': e.get('name', 'setup'), 'argv': e['argv'], 'code': e['code']}
                                      for e in cycle['verification']],
                           'review': cycle['review'], 'worktree': cycle['work']}})
            self.flush()
            self.finish(cycle, 'merged')
            try:
                self.repo.discard_clean_worktree(cycle['work'])
            except (Blocked, ConfigError) as exc:
                self.state.event(cleanup_warning=str(exc))

    def implement(self, cycle):
        from pathlib import Path
        work = Path(cycle['work'])
        while cycle['attempts'] < 3:
            self.checkpoint(cycle, 'implement', attempts=cycle['attempts'] + 1)
            try:
                # Always rerun setup on resume; supported setup commands must be idempotent.
                for argv in cycle['commands']['setup']:
                    self.runner.run(argv, work, self.config.command_timeout)
                ticket = self.jira.issue(cycle['key'])  # Honor human edits immediately before acting.
                for record in self.state.get('recommendations', {}).values():
                    if record.get('key') == cycle['key']:
                        ticket = {**ticket, 'planned_recommendation': record['recommendation'],
                                  'note': 'Current Jira fields override the original recommendation when edited.'}
                        break
                if ('self-improver-blocked' in ticket['fields'].get('labels', [])
                        or ticket['fields']['status'].get('statusCategory', {}).get('key') == 'done'):
                    raise Blocked('Ticket was blocked in Jira before implementation.')
                result = self.agent.implement(ticket, self.repo.context(work), cycle['commands'], cycle['feedback'], work)
                validate_schema(result, IMPLEMENT_SCHEMA)
                if result['blocked']:
                    self.block(cycle, result['reason'] or 'Agent reported unresolved requirements.')
                    return
                if not result['acceptance_evidence'] or not result['documentation'] or not result['tests_added']:
                    raise Blocked('Implementation must supply acceptance, documentation and test evidence.')
                changed = set(self.repo.git('diff', '--name-only', cycle['base'], cwd=work).splitlines())
                changed.update(self.repo.git('ls-files', '--others', '--exclude-standard', cwd=work).splitlines())
                for name in [*result['documentation'], *result['tests_added']]:
                    path = work / name
                    if (Path(name).is_absolute() or not path.resolve().is_relative_to(work.resolve())
                            or not path.is_file() or path.is_symlink() or name.replace('\\', '/') not in changed):
                        raise Blocked(f'Claimed documentation/test evidence is not a changed file: {name}')
                ticket_commands = {'setup': [], 'checks': result['ticket_checks']}
                validate_commands(ticket_commands)
                evidence = self.run_checks(cycle['commands'], work) + self.run_checks(ticket_commands, work)
                commit = self.repo.commit(work, cycle['key'], result['summary'])
                if commit == cycle['base']:
                    raise Blocked('Agent made no source changes.')
                if self.repo.git('status', '--porcelain', cwd=work):
                    raise Blocked('Worktree changed during commit; verified snapshot is no longer clean.')
                # Hooks can change staged content during commit. Test the committed snapshot too.
                evidence += self.run_checks(cycle['commands'], work) + self.run_checks(ticket_commands, work)
                if self.repo.head(work) != commit or self.repo.git('status', '--porcelain', cwd=work):
                    raise Blocked('Post-commit validation modified the verified snapshot.')
                review = self.agent.review(ticket, cycle['base'], commit, self.repo.context(work),
                                          {'implementation': result, 'checks': evidence,
                                           'baseline': cycle['baseline_evidence']}, work)
                validate_schema(review, REVIEW_SCHEMA)
                if not review['approved'] or review['findings']:
                    raise Blocked('Independent review rejected change: ' + canonical(review))
                if self.repo.head(work) != commit or self.repo.git('status', '--porcelain', cwd=work):
                    raise Blocked('Worktree changed during independent review.')
                self.checkpoint(cycle, 'merging', commit=commit, implementation=result,
                                verification=evidence, review=review)
                return
            except Blocked as exc:
                self.checkpoint(cycle, 'implement', feedback=str(exc))
            except TransientError:
                self.checkpoint(cycle, 'implement', attempts=cycle['attempts'] - 1)
                raise
        self.block(cycle, cycle['feedback'] or 'Implementation interrupted too many times.')

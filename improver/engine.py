"""Checkpointed review → Jira → worktree → verify → integrate state machine."""
from __future__ import annotations

import hashlib
import re
import uuid

from .agent import validate_commands, validate_recommendations, validate_schema, IMPLEMENT_SCHEMA, REVIEW_SCHEMA
from .core import Blocked, ConfigError, TransientError, canonical, recommendation_id, redact
from .jira import BLOCKED_LABEL, JiraNotFound, has_description, issue_description


class ApprovalChanged(RuntimeError):
    """A selected issue no longer authorizes work on the approved scope."""


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
            try:
                if item['kind'] == 'backlog':
                    self.jira.place_backlog(item['key'])
                elif item['kind'] == 'scope':
                    ticket = self.jira.issue(item['key'])
                    if ticket['key'] == item['key'] and self.scope_revision(ticket) == item['revision']:
                        if not self.is_todo(ticket) or not self.selectable(ticket):
                            continue
                        self.deliver_comment(identity, item, f'Review the current title and description, then reply with:\n'
                                             f'approve {item["revision"]}')
                elif item['kind'] == 'done':
                    result = item['result']
                    checks = '\n'.join(f'- {check["name"]}: {"passed" if check["code"] == 0 else "failed"}'
                                       for check in {check['name']: check for check in result['checks']}.values())
                    self.deliver_comment(identity, item, f'Implementation summary\n{result["summary"]}\n\n'
                                         f'Checks on the implementation\n{checks}\n\nCommit: {result["commit"]}')
                    self.jira.complete(item['key'], result)
                elif item['kind'] == 'blocked':
                    self.jira.block(item['key'], item['reason'])
                else:
                    raise ConfigError(f'Unknown outbox operation {item["kind"]}.')
            except JiraNotFound:
                self.state.event(ticket=item['key'], reason=f'Jira issue unavailable during {item["kind"]}.')
            except Blocked as exc:
                self.state.event(ticket=item['key'], reason=str(exc))
            self.state.acknowledge(identity)

    def deliver_comment(self, identity, item, text):
        if item.get('comment_delivered'):
            return
        key = item['key']
        marker = f'{self.config.app_id}:{identity}'
        if not self.jira.comment_exists(self.jira.comments(key), marker):
            if item.get('comment_attempted'):
                raise TransientError(f'{key}: uncertain Jira comment {identity}. Check Jira, then use resolve-comment.', 60)
            item['comment_attempted'] = True
            self.state.update_pending(identity, item)
            try:
                self.jira.add_comment(key, redact(text), marker)
            except ConfigError:
                item['comment_attempted'] = False
                self.state.update_pending(identity, item)
                raise
        item['comment_delivered'] = True
        self.state.update_pending(identity, item)

    def is_todo(self, ticket):
        return ticket['fields']['status']['name'] == self.config.jira['statuses']['backlog']

    def project_todo(self):
        issues = self.jira.todo()
        recent = self.state.get('recent_jira_create')
        if not issues and recent:
            try:
                ticket = self.jira.issue(recent)
                if ticket['key'] == recent and self.is_todo(ticket):
                    return [ticket]
            except JiraNotFound:
                pass
            self.state.set('recent_jira_create', None)
        return issues

    def publish(self, cycle):
        records = self.state.get('recommendations', {})
        created = False
        for fingerprint in cycle['fingerprints']:
            record = records.get(fingerprint)
            if record is None:
                continue
            if not record.get('key'):
                if record.get('attempted'):
                    matches = self.jira.issues(f'labels = "self-improver-fp-{fingerprint}"')
                    if len(matches) != 1:
                        raise TransientError(
                            f'Uncertain Jira creation {fingerprint}: found {len(matches)} issues. '
                            'No create retry will be made. Check Jira, then use resolve-create.', 60)
                    record['key'] = matches[0]['key']
                else:
                    if created or self.project_todo():
                        # A later empty backlog can reconsider recommendations that were never submitted.
                        records.pop(fingerprint)
                        self.state.set('recommendations', records)
                        continue
                    record['attempted'] = True
                    self.state.set('recommendations', records)
                    try:
                        record['key'] = self.jira.create(record['recommendation'], fingerprint, record['base'])
                    except ConfigError:
                        # Jira rejected this request definitively (4xx or invalid local config).
                        record['attempted'] = False
                        self.state.set('recommendations', records)
                        raise
                self.state.set_many({'recommendations': records, 'recent_jira_create': record['key']})
            created = True
            self.state.enqueue(f'backlog:{record["key"]}', {'kind': 'backlog', 'key': record['key']})
        self.flush()

    def new_cycle(self):
        if self.project_todo():
            cycle = {'id': uuid.uuid4().hex[:12], 'base': self.repo.head()}
            self.checkpoint(cycle, 'select')
            return cycle
        context = self.repo.context()
        backlog = self.jira.issues(project_wide=True)
        records = self.state.get('recommendations', {})
        result = self.agent.recommend(context, self.backlog_context(backlog, records),
                                      self.config.jira['priorities'], self.repo.root)
        recs = validate_recommendations(result, len(self.config.jira['priorities']))
        titles = {' '.join(r['recommendation']['title'].casefold().split()) for r in records.values()}
        titles.update(' '.join(i['fields']['summary'].casefold().split()) for i in backlog)
        fingerprints = []
        for rec in sorted(recs, key=lambda rec: rec['priority_rank']):
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
            records[fingerprint] = {'recommendation': rec, 'base': context['commit'],
                                    'attempted': False, 'sequence': len(records)}
            fingerprints.append(fingerprint)
            break
        cycle = {'id': uuid.uuid4().hex[:12], 'base': context['commit'], 'fingerprints': fingerprints}
        # Recommendations and the publish checkpoint must become durable together.
        cycle['phase'] = 'publish'
        self.state.set_many({'recommendations': records, 'cycle': cycle})
        self.checkpoint(cycle, 'publish')
        return cycle

    def scope_revision(self, ticket):
        fields = ticket['fields']
        scope = [fields['summary'], fields.get('description'), self.jira.last_field_edit(ticket['key'], {'summary', 'description'})]
        digest = hashlib.sha256(canonical(scope).encode()).hexdigest()
        identity = f'scope:{ticket.get("id", ticket["key"])}'
        record = self.state.get(identity, {})
        if record.get('digest') != digest:
            record = {'digest': digest, 'revision': uuid.uuid4().hex[:12]}
            self.state.set(identity, record)
        return record['revision']

    def selectable(self, ticket):
        labels = ticket['fields'].get('labels', [])
        return BLOCKED_LABEL not in labels and all(
            label == self.jira.label for label in labels if label.startswith('self-improver-app-'))

    def approved_ticket(self):
        approved, approvals, waiting = [], {}, []
        for match in self.project_todo():
            try:
                ticket = self.jira.issue(match['key'])
                if ticket['key'] != match['key'] or not self.is_todo(ticket) or not self.selectable(ticket):
                    continue
                key, fields = ticket['key'], ticket['fields']
                revision = self.scope_revision(ticket)
                if not has_description(fields.get('description')):
                    waiting.append(f'{key} waiting for a description')
                    continue
                if (fields.get('priority') or {}).get('id') not in self.config.jira['priorities']:
                    waiting.append(f'{key} has an unmapped Jira priority')
                    continue
                identity = f'approval:{key}:{revision}'
                comments = list(self.jira.comments(key))
                marker = f'{self.config.app_id}:{identity}'
                if self.jira.comment_exists(comments, marker):
                    approval = self.jira.approval(comments, revision)
                    if approval:
                        approvals[key] = {'revision': revision, 'comment': approval}
                        approved.append(ticket)
                        continue
                self.state.enqueue(identity, {'kind': 'scope', 'key': key, 'revision': revision})
                waiting.append(f'{key} waiting for approval')
            except JiraNotFound:
                continue
        self.flush()
        selected = self.jira.select(approved)
        return selected, approvals.get(selected['key']) if selected else None, waiting

    def require_approval(self, cycle, *statuses):
        key = cycle['key']
        approval = cycle.get('approval')
        if not approval:
            raise ApprovalChanged(f'{key}: return the ticket to To Do and approve its scope.')
        try:
            ticket = self.jira.issue(key)
            if ticket['key'] != key or not self.selectable(ticket) or ticket['fields']['status']['name'] not in statuses:
                raise ApprovalChanged(f'{key}: Jira status or labels no longer allow work.')
            if self.scope_revision(ticket) != approval['revision']:
                raise ApprovalChanged(f'{key}: scope changed. Return the ticket to To Do and approve the new revision.')
            comments = (c for c in self.jira.comments(key) if str(c['id']) == approval['comment'])
            if not self.jira.approval(comments, approval['revision']):
                raise ApprovalChanged(f'{key}: approval withdrawn. Return the ticket to To Do and approve its scope.')
        except JiraNotFound as exc:
            raise ApprovalChanged(f'{key}: Jira issue was deleted or is no longer visible.') from exc
        return {'key': key, 'fields': {name: ticket['fields'].get(name) for name in ('summary', 'description')}}

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
        try:
            self.advance()
        except ApprovalChanged as exc:
            cycle = self.state.get('cycle')
            cycle['reason'] = str(exc)
            print(cycle['reason'], flush=True)
            self.finish(cycle, 'approval interrupted')

    def advance(self):
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
            selected, approval, waiting = self.approved_ticket()
            if not selected:
                if waiting:
                    cycle['reason'] = '; '.join(waiting)
                    print(cycle['reason'], flush=True)
                self.finish(cycle, 'waiting for Jira' if waiting else 'no eligible work')
                return
            self.checkpoint(cycle, 'baseline', key=selected['key'], approval=approval,
                            work_name=f'ticket-{selected["key"]}-{cycle["id"]}', attempts=0)
        if cycle['phase'] == 'blocked':
            self.finish_block(cycle)
            return
        statuses = self.config.jira['statuses']
        if cycle['phase'] == 'baseline':
            self.require_approval(cycle, statuses['backlog'])
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
                self.checkpoint(cycle, 'starting', baseline_evidence=evidence, work=str(work), feedback='')
            except Blocked as exc:
                self.require_approval(cycle, statuses['backlog'])
                self.block(cycle, exc)
                return
        if cycle['phase'] == 'starting':
            expected = [statuses['backlog']]
            if cycle.get('transition_attempted'):
                expected.append(statuses['in_progress'])
            self.require_approval(cycle, *expected)
            self.checkpoint(cycle, 'starting', transition_attempted=True)
            try:
                self.jira.transition(cycle['key'], statuses['in_progress'], expected=expected)
            except JiraNotFound as exc:
                raise ApprovalChanged(f'{cycle["key"]}: Jira issue is no longer visible.') from exc
            except Blocked as exc:
                raise ApprovalChanged(str(exc)) from exc
            self.checkpoint(cycle, 'implement')
        if cycle['phase'] == 'implement':
            self.implement(cycle)
        if cycle['phase'] == 'merging':
            # A crash after Git integration must not cause a second implementation.
            if not self.repo.contains(cycle['commit'], self.config.target_branch):
                self.require_approval(cycle, statuses['in_progress'])
                try:
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
        status = self.config.jira['statuses']['in_progress']
        self.require_approval(cycle, status)
        work = Path(cycle['work'])
        while cycle['attempts'] < 3:
            self.checkpoint(cycle, 'implement', attempts=cycle['attempts'] + 1)
            try:
                # Always rerun setup on resume; supported setup commands must be idempotent.
                for argv in cycle['commands']['setup']:
                    self.runner.run(argv, work, self.config.command_timeout)
                ticket = self.require_approval(cycle, status)
                for record in self.state.get('recommendations', {}).values():
                    if record.get('key') == cycle['key']:
                        rec = record['recommendation']
                        if (ticket['fields']['summary'] == rec['title'] and
                                ticket['fields']['description'] == issue_description(rec, record['base'])):
                            ticket['planned_recommendation'] = {'skills': rec['skills']}
                        break
                result = self.agent.implement(ticket, self.repo.context(work), cycle['commands'], cycle['feedback'], work)
                validate_schema(result, IMPLEMENT_SCHEMA)
                if result['blocked']:
                    self.require_approval(cycle, status)
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
        self.require_approval(cycle, status)
        self.block(cycle, cycle['feedback'] or 'Implementation interrupted too many times.')

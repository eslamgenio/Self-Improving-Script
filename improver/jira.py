"""Jira Cloud REST integration. Never automatically retries ambiguous writes."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .core import Blocked, ConfigError, TransientError, canonical, redact


ORIGIN = 'self-improver'
BLOCKED_LABEL = 'self-improver-blocked'
COMMENT_MARKER = 'self-improver-operation'


class JiraNotFound(ConfigError):
    """Jira cannot show the requested issue or resource."""


def has_description(value):
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return has_description(value.get('text')) or any(has_description(node) for node in value.get('content', []))
    return False


def approval_text(body):
    """Read a standalone text paragraph, excluding quotes and code blocks."""
    if not isinstance(body, dict) or body.get('type') != 'doc':
        return ''
    paragraphs = body.get('content', [])
    if len(paragraphs) != 1 or paragraphs[0].get('type') != 'paragraph':
        return ''
    nodes = paragraphs[0].get('content', [])
    if any(node.get('type') != 'text' for node in nodes):
        return ''
    return ''.join(node.get('text', '') for node in nodes).strip()


def adf(text):
    return {'type': 'doc', 'version': 1, 'content': [
        {'type': 'paragraph', 'content': [{'type': 'text', 'text': line}]} if line else
        {'type': 'paragraph', 'content': []} for line in str(text).split('\n')]}


def issue_description(rec, base):
    sections = [f'Application baseline: {base}']
    for key, value in rec.items():
        if key in {'title', 'kind', 'priority_rank'}:
            continue
        text = '\n'.join(f'- {item}' for item in value) if isinstance(value, list) else str(value)
        sections.append(f'{key.replace("_", " ").title()}\n{text}')
    return adf('\n\n'.join(sections))


def retry_seconds(value):
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward an Authorization header to another host.


class Jira:
    def __init__(self, config, transport=None):
        self.config = config
        self.settings = config.jira
        self.label = f'self-improver-app-{config.app_id}'
        self._transport = transport
        self.opener = build_opener(NoRedirect())
        self._account_id = None

    def base_url(self):
        base = self.settings.get('base_url', '').rstrip('/')
        try:
            parsed = urlparse(base)
        except ValueError as exc:
            raise ConfigError('Invalid Jira base URL.') from exc
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise ConfigError('jira.base_url must be an HTTPS Jira site or api.atlassian.com/ex/jira/<cloudId>.')
        return base

    def request(self, method, path, body=None):
        if self._transport:
            return self._transport(method, path, body)
        base = self.base_url()
        email, token = os.environ.get('JIRA_EMAIL'), os.environ.get('JIRA_API_TOKEN')
        if not email or not token:
            raise ConfigError('Set jira_email and jira_api_token in setup-scrum.json. For repository settings, set JIRA_EMAIL and JIRA_API_TOKEN in the environment.')
        auth = base64.b64encode(f'{email}:{token}'.encode()).decode()
        request = Request(base + path, method=method,
                          data=canonical(body).encode() if body is not None else None,
                          headers={'Authorization': f'Basic {auth}', 'Accept': 'application/json',
                                   'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=45) as response:
                data = response.read()
                return json.loads(data) if data else {}
        except HTTPError as exc:
            detail = redact(exc.read().decode(errors='replace'))[:2000]
            if exc.code == 429 or exc.code >= 500:
                raise TransientError(f'Jira HTTP {exc.code}: {detail}', retry_seconds(exc.headers.get('Retry-After'))) from exc
            if exc.code == 404:
                raise JiraNotFound(f'Jira HTTP 404: {detail}') from exc
            raise ConfigError(f'Jira HTTP {exc.code}: {detail}') from exc
        except (URLError, TimeoutError, ConnectionError) as exc:
            raise TransientError(f'Jira connection failed: {exc}') from exc
        except ValueError as exc:
            raise TransientError('Jira returned malformed JSON; pending writes require reconciliation.') from exc

    def issues(self, extra='', *, project_wide=False):
        project = self.settings.get('project', '')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', project):
            raise ConfigError('jira.project must be a Jira project key.')
        jql = f'project = "{project}"'
        if not project_wide:
            jql += f' AND labels = "{ORIGIN}" AND labels = "{self.label}"'
        if extra:
            jql += f' AND ({extra})'
        items, token, seen = [], None, set()
        while True:
            body = {'jql': jql, 'maxResults': 100,
                    'fields': ['summary', 'description', 'priority', 'created', 'labels', 'status', 'issuetype', 'issuelinks']}
            if token:
                body['nextPageToken'] = token
            page = self.request('POST', '/rest/api/3/search/jql', body)
            items.extend(page.get('issues', []))
            token = page.get('nextPageToken')
            if page.get('isLast') or not token:
                return items
            if token in seen:
                raise TransientError('Jira repeated a pagination token.')
            seen.add(token)

    def todo(self):
        status = json.dumps(self.settings['statuses']['backlog'], ensure_ascii=False)
        return self.issues(f'status = {status}', project_wide=True)

    def issue(self, key):
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*-\d+', key):
            raise ConfigError('Invalid Jira issue key.')
        return self.request('GET', f'/rest/api/3/issue/{key}')

    def account_id(self):
        if self._account_id is None:
            self._account_id = self.request('GET', '/rest/api/3/myself').get('accountId')
            if not self._account_id:
                raise ConfigError('Jira did not identify the authenticated account for scope approval.')
        return self._account_id

    def last_field_edit(self, key, fields):
        # Jira returns issue changelogs in date order, oldest first.
        latest, start = None, 0
        while True:
            page = self.request('GET', f'/rest/api/3/issue/{key}/changelog?startAt={start}&maxResults=100')
            entries = page['values']
            for entry in entries:
                if any((change.get('fieldId') or change.get('field')) in fields
                       for change in entry['items']):
                    latest = entry['id']
            start += len(entries)
            if page.get('isLast') or start >= page['total']:
                return latest
            if not entries:
                raise TransientError(f'{key}: Jira changelog pagination did not advance.')

    def comments(self, key):
        start = 0
        while True:
            query = urlencode({'startAt': start, 'maxResults': 100, 'expand': 'properties'})
            page = self.request('GET', f'/rest/api/3/issue/{key}/comment?{query}')
            comments = page['comments']
            yield from comments
            start += len(comments)
            if start >= page['total']:
                return
            if not comments:
                raise TransientError(f'{key}: Jira comment pagination did not advance.')

    def add_comment(self, key, text, marker):
        self.request('POST', f'/rest/api/3/issue/{key}/comment',
                     {'body': adf(text), 'properties': [{'key': COMMENT_MARKER, 'value': {'marker': marker}}]})

    def comment_exists(self, comments, marker):
        return any(comment.get('author', {}).get('accountId') == self.account_id()
                   and any(prop['key'] == COMMENT_MARKER and prop['value'] in (marker, {'marker': marker})
                           for prop in comment.get('properties', [])) for comment in comments)

    def approval(self, comments, revision):
        account = self.account_id()
        return next((str(comment['id']) for comment in comments
                     if comment.get('author', {}).get('accountId') == account
                     and comment.get('updateAuthor', comment['author']).get('accountId') == account
                     and approval_text(comment['body']) == f'approve {revision}'), None)

    def select(self, issues):
        priorities = self.settings['priorities']
        eligible = []
        for issue in issues:
            fields = issue['fields']
            if BLOCKED_LABEL in fields.get('labels', []) or fields.get('status', {}).get('statusCategory', {}).get('key') == 'done':
                continue
            priority = (fields.get('priority') or {}).get('id')
            if priority not in priorities:
                raise ConfigError(f'{issue["key"]} has an unmapped Jira priority {priority}; update jira.priorities.')
            eligible.append(issue)
        return min(eligible, key=lambda i: (priorities.index(i['fields']['priority']['id']),
                                           i['fields']['created'], i['key'])) if eligible else None

    def create(self, rec, fingerprint, base):
        fields = dict(self.settings.get('custom_fields', {}))
        fields.update({'project': {'key': self.settings['project']}, 'summary': rec['title'],
                       'description': issue_description(rec, base),
                       'issuetype': {'id': self.settings['issue_types'][rec['kind']]},
                       'priority': {'id': self.settings['priorities'][rec['priority_rank']]},
                       'labels': [ORIGIN, self.label, f'self-improver-fp-{fingerprint}']})
        if rec.get('dependencies'):
            fields['labels'].append(BLOCKED_LABEL)
        return self.request('POST', '/rest/api/3/issue', {'fields': fields})['key']

    def place_backlog(self, key):
        board = int(self.settings['board_id'])
        ticket = self.issue(key)
        status = ticket['fields']['status']
        backlog = self.settings['statuses']['backlog']
        if ticket['key'] != key:
            return
        if status['name'] != backlog and (status.get('statusCategory', {}).get('key') != 'new'
                                          or self.last_field_edit(key, {'status'})):
            return
        # A backlog call cannot add an issue excluded by the board's filter.
        query = urlencode({'jql': f'key = "{key}"', 'fields': 'key'})
        result = self.request('GET', f'/rest/agile/1.0/board/{board}/issue?{query}')
        if not any(i['key'] == key for i in result.get('issues', [])):
            raise TransientError(f'{key} is not visible on board {board}; check the board filter or wait for indexing.')
        self.request('POST', f'/rest/agile/1.0/backlog/{board}/issue', {'issues': [key]})
        self.transition(key, backlog, expected=(status['name'], backlog))

    def transition(self, key, destination, *, expected=None):
        current = self.issue(key)['fields']['status']['name']
        if expected is not None and current not in expected:
            raise Blocked(f'{key}: Jira status changed to {current} before transition.')
        if current == destination:
            return
        result = self.request('GET', f'/rest/api/3/issue/{key}/transitions?expand=transitions.fields')
        matches = [t for t in result.get('transitions', []) if t['to']['name'] == destination]
        if len(matches) != 1:
            raise ConfigError(f'{key}: no unique transition from {current} to {destination}. Configure a directly reachable workflow.')
        required = {name for name, meta in matches[0].get('fields', {}).items()
                    if meta.get('required') and not meta.get('hasDefaultValue')}
        fields = {k: v for k, v in self.settings.get('transition_fields', {}).get(destination, {}).items()}
        if required - fields.keys():
            raise ConfigError(f'{key}: transition needs fields {sorted(required - fields.keys())}. Set jira.transition_fields.')
        body = {'transition': {'id': matches[0]['id']}}
        if fields:
            body['fields'] = fields
        self.request('POST', f'/rest/api/3/issue/{key}/transitions', body)

    def block(self, key, reason):
        # Jira update operations make this safe to retry without adding duplicate comments.
        self.request('PUT', f'/rest/api/3/issue/{key}', {'update': {'labels': [{'add': BLOCKED_LABEL}]}})
        self.request('PUT', f'/rest/api/3/issue/{key}/properties/self-improver-result', {'blocked': True, 'reason': reason})

    def complete(self, key, result):
        self.request('PUT', f'/rest/api/3/issue/{key}/properties/self-improver-result', result)
        statuses = self.settings['statuses']
        self.transition(key, statuses['done'], expected=(statuses['in_progress'], statuses['done']))

    def doctor(self):
        self.account_id()
        project = self.settings.get('project', '')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', project):
            raise ConfigError('Set jira.project to a valid project key.')
        if not self.settings.get('board_id'):
            raise ConfigError('Set jira.board_id to a Jira Software board with backlog enabled.')
        self.request('GET', f'/rest/agile/1.0/board/{int(self.settings["board_id"])}')
        permission_names = ['BROWSE_PROJECTS', 'CREATE_ISSUES', 'EDIT_ISSUES', 'TRANSITION_ISSUES', 'SCHEDULE_ISSUES',
                            'ADD_COMMENTS']
        query = urlencode({'projectKey': project, 'permissions': ','.join(permission_names)})
        permissions = self.request('GET', '/rest/api/3/mypermissions?' + query).get('permissions', {})
        missing = [p for p in permission_names if not permissions.get(p, {}).get('havePermission')]
        if missing:
            raise ConfigError(f'Jira permissions missing: {missing}')
        types = self.settings.get('issue_types', {})
        if set(types) != {'bug', 'improvement', 'feature'} or not all(types.values()):
            raise ConfigError('Map bug, improvement and feature to Jira issue type IDs.')
        statuses = self.settings.get('statuses', {})
        if not all(statuses.get(s) for s in ('backlog', 'in_progress', 'done')):
            raise ConfigError('Map backlog, in_progress and done to Jira status names.')
        workflows = self.request('GET', f'/rest/api/3/project/{quote(project)}/statuses')
        for type_id in set(types.values()):
            records = [w for w in workflows if str(w['id']) == str(type_id)]
            available = {s['name'] for w in records for s in w.get('statuses', [])}
            if set(statuses.values()) - available:
                raise ConfigError(f'Issue type {type_id} lacks configured statuses: {set(statuses.values()) - available}')
            endpoint = f'/rest/api/3/issue/createmeta/{project}/issuetypes/{type_id}'
            fields, start = [], 0
            while True:
                page = self.request('GET', f'{endpoint}?startAt={start}&maxResults=100')
                values = page.get('fields', page.get('values', []))
                fields.extend(values)
                start += len(values)
                if page.get('isLast', start >= page.get('total', start)):
                    break
                if not values:
                    raise ConfigError('Jira create metadata pagination did not advance.')
            field_map = {f.get('fieldId', f.get('key')): f for f in fields}
            supplied = {'project', 'summary', 'description', 'issuetype', 'priority', 'labels'} | set(self.settings.get('custom_fields', {}))
            required = {key for key, meta in field_map.items() if meta.get('required') and not meta.get('hasDefaultValue')}
            if required - supplied:
                raise ConfigError(f'Issue type {type_id} requires custom fields: {sorted(required - supplied)}')
            if {'priority', 'labels', 'description'} - field_map.keys():
                raise ConfigError(f'Issue type {type_id} must expose priority, labels and description on its create screen.')
            allowed = {str(p['id']) for p in field_map['priority'].get('allowedValues', [])}
            if allowed and set(self.settings['priorities']) - allowed:
                raise ConfigError(f'Invalid priority mapping for issue type {type_id}. Allowed: {sorted(allowed)}')
        return {'jira': 'OK', 'workflow': 'Status names validated; transition availability is checked per issue.'}

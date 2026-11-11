from contextlib import redirect_stdout
from dataclasses import asdict
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from improver.cli import main
from improver.core import Config, Runner, State, TransientError
from improver.engine import Engine
from improver.git import Repository
from improver.jira import COMMENT_MARKER, Jira, JiraNotFound, adf, issue_description
from test_git_agent import git, make_repo


def recommendation(title, rank=1):
    return dict(title=title, kind='feature', priority_rank=rank, priority_reason='User value',
                business_value='Improve user workflow', current_behavior='Missing feature',
                desired_behavior='Feature works', evidence=['README.md:1'],
                acceptance_criteria=['Feature runs'], implementation=['Implement and test'],
                skills=['test-driven-development'], effort='Small', dependencies=[], risks=['Low'],
                validation=['Run regression and runtime checks'], migration_rollback='Revert commit')


class MemoryJira(Jira):
    def __init__(self, config):
        super().__init__(config, transport=lambda *a: {})
        self.items = []
        self.fail_create = False
        self.fail_complete = False
        self.completed = []
        self.blocked = []
        self.comment_items = {}
        self.history = {}
        self.lose_comment_kind = None

    def issues(self, extra='', *, project_wide=False):
        return [i for i in self.items if not extra or any(label in extra for label in i['fields']['labels'] if 'fp-' in label)]

    def ready(self):
        return [i for i in self.items if i['fields']['status']['name'] == self.approval_status]

    def add_ticket(self, title, description, priority='1', labels=()):
        key = f'APP-{len(self.comment_items)+1}'
        self.items.append({'key': key, 'fields': {'summary': title, 'description': description,
                           'priority': {'id': priority}, 'created': key, 'labels': list(labels),
                           'status': {'name': 'Open', 'statusCategory': {'key': 'new'}}}})
        self.comment_items[key] = []
        self.history[key] = []
        return key

    def create(self, rec, fp, base):
        key = self.add_ticket(rec['title'], issue_description(rec, base),
                              self.settings['priorities'][rec['priority_rank']],
                              ['self-improver', self.label, f'self-improver-fp-{fp}'])
        if self.fail_create:
            self.fail_create = False
            raise TransientError('Response lost after create')
        return key

    def issue(self, key):
        for item in self.items:
            if item['key'] == key:
                return item
        raise JiraNotFound(key)

    def changelog(self, key):
        self.issue(key)
        return iter(self.history[key])

    def record_change(self, key, **fields):
        history = self.history[key]
        history.append({'id': str(len(history) + 1), 'items': [
            {'fieldId': field, **change} for field, change in fields.items()
        ]})

    def account_id(self):
        return 'operator'

    def comments(self, key):
        self.issue(key)
        return iter(self.comment_items[key])

    def add_comment(self, key, text, marker=None, author='operator'):
        comments = self.comment_items[key]
        comments.append({'id': str(len(comments) + 1), 'author': {'accountId': author}, 'body': adf(text),
                         'properties': [{'key': COMMENT_MARKER, 'value': marker}] if marker else []})
        if self.lose_comment_kind and marker and f':{self.lose_comment_kind}:' in marker:
            self.lose_comment_kind = None
            raise TransientError('Comment response lost')

    def approve(self, key):
        self.transition(key, self.approval_status, expected=('Open',))

    def place_backlog(self, key):
        pass

    def transition(self, key, destination, *, expected=None):
        if expected:
            assert self.issue(key)['fields']['status']['name'] in expected
        status = self.issue(key)['fields']['status']
        if status['name'] != destination:
            self.record_change(key, status={'fromString': status['name'], 'toString': destination})
            status['name'] = destination

    def complete(self, key, result):
        if self.fail_complete:
            self.fail_complete = False
            raise TransientError('offline')
        self.issue(key)['fields']['status']['statusCategory']['key'] = 'done'
        self.transition(key, 'Done')
        self.completed.append(key)

    def block(self, key, reason):
        self.transition(key, self.blocked_status, expected=(self.approval_status, 'Working', self.blocked_status))
        self.issue(key)['fields']['labels'].append('self-improver-blocked')
        self.blocked.append(key)


class FakeAgent:
    def __init__(self):
        self.contexts = []
        self.calls = 0
        self.fail_review = False

    def discover(self, context, cwd):
        return {'setup': [], 'checks': [
            {'name': 'test', 'kind': 'test', 'argv': [sys.executable, '-c', 'assert 1 + 1 == 2']},
            {'name': 'runtime', 'kind': 'runtime', 'argv': [sys.executable, '-c', 'print("runtime")']}], 'reason': 'fixture'}

    def recommend(self, context, backlog, priorities, cwd):
        self.contexts.append(context['commit'])
        self.calls += 1
        return {'recommendations': [recommendation('Older feature', 1), recommendation('First feature', 0)] if self.calls == 1
                else [recommendation('First feature', 0), recommendation('New lower priority', 2)], 'notes': ''}

    def implement(self, ticket, context, commands, feedback, cwd):
        (cwd / f'{ticket["key"]}.txt').write_text('feature')
        (cwd / 'README.md').write_text(f'Updated application documentation for {ticket["key"]}')
        (cwd / 'tests').mkdir(exist_ok=True)
        test_name = f'tests/test_{ticket["key"].replace("-", "_")}.py'
        (cwd / test_name).write_text('def test_feature():\n    assert True\n')
        return {'blocked': False, 'reason': '', 'summary': 'implemented', 'acceptance_evidence': ['Works'],
                'documentation': ['README.md'], 'tests_added': [test_name], 'ticket_checks': commands['checks']}

    def review(self, *args):
        return {'approved': not self.fail_review, 'findings': ['Needs repair'] if self.fail_review else [], 'reason': ''}


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='engine space ')
        self.root = Path(self.temp.name) / 'app'
        make_repo(self.root)
        self.repo = Repository(self.root, Runner())
        self.repo.home.mkdir(parents=True)
        self.state = State(self.repo.home / 'state.db')
        self.cfg = Config(app_id='demo', jira={'project': 'APP', 'priorities': ['1', '2', '3'],
                         'statuses': {'in_progress': 'Working', 'done': 'Done', 'backlog': 'Open'}})
        self.jira = MemoryJira(self.cfg)
        self.agent = FakeAgent()
        self.engine = Engine(self.cfg, self.repo, self.state, self.jira, self.agent, Runner(self.state))

    def tearDown(self):
        self.state.__exit__()
        self.temp.cleanup()

    def create_and_approve(self):
        self.engine.cycle()
        self.jira.approve('APP-1')


class EngineTests(EngineCase):
    def test_duplicate_batch_retries_with_matching_jira_keys_in_same_cycle(self):
        key = self.jira.add_ticket('Existing feature', adf('Scope'))
        duplicate = {'recommendations': [recommendation('existing  feature')], 'notes': ''}
        fresh = {'recommendations': [recommendation('New feature'), recommendation('Another feature')], 'notes': ''}
        with patch.object(self.agent, 'recommend', side_effect=[duplicate, fresh]) as recommend:
            self.engine.cycle()
        self.assertEqual(recommend.call_count, 2)
        self.assertEqual(recommend.call_args.args[1]['duplicate_recommendations'], [
            {'title': 'existing  feature', 'matching_jira_tickets': [key]},
        ])
        self.assertEqual([issue['fields']['summary'] for issue in self.jira.items],
                         ['Existing feature', 'New feature'])

    def test_duplicate_retry_does_not_loop_or_publish_a_duplicate(self):
        self.jira.add_ticket('Existing feature', adf('Scope'))
        result = {'recommendations': [recommendation('Existing feature')], 'notes': ''}
        with patch.object(self.agent, 'recommend', return_value=result) as recommend:
            self.engine.cycle()
        self.assertEqual(recommend.call_count, 2)
        self.assertEqual(len(self.jira.items), 1)

    def test_new_ticket_waits_for_jira_approval(self):
        base = self.repo.head()
        self.engine.cycle()
        self.assertEqual(len(self.jira.items), 1)
        self.assertEqual(self.jira.items[0]['fields']['summary'], 'First feature')
        self.assertEqual(self.repo.head(), base)
        self.assertEqual(self.jira.completed, [])

    def test_two_approved_cycles_review_updated_code(self):
        initial = self.repo.head()
        self.create_and_approve()
        self.engine.cycle()
        first = self.repo.head()
        self.engine.cycle()
        self.jira.approve('APP-2')
        self.engine.cycle()
        self.assertNotEqual(initial, first)
        self.assertEqual(self.agent.contexts, [initial, first])
        self.assertEqual(self.jira.completed, ['APP-1', 'APP-2'])
        self.assertEqual(len(self.jira.items), 2)
        self.assertIsNone(self.state.get('cycle'))

    def test_uncertain_create_is_reconciled_without_duplicate(self):
        self.jira.fail_create = True
        with self.assertRaises(TransientError):
            self.engine.cycle()
        self.engine.cycle()
        self.assertEqual(len(self.jira.items), 1)
        self.assertEqual(self.jira.completed, [])
        self.jira.approve('APP-1')
        self.engine.cycle()
        self.assertEqual(self.jira.completed, ['APP-1'])

    def test_merged_change_is_not_reimplemented_when_jira_is_offline(self):
        self.create_and_approve()
        self.jira.fail_complete = True
        with self.assertRaises(TransientError):
            self.engine.cycle()
        merged = self.repo.head()
        self.assertTrue(self.state.pending())
        self.engine.cycle()
        self.assertEqual(self.repo.head(), merged)
        self.assertEqual(self.jira.completed, ['APP-1'])
        self.assertEqual(sum(':done:' in prop['value'] for c in self.jira.comments('APP-1')
                             for prop in c['properties']), 1)

    def test_review_failure_blocks_without_merging(self):
        self.agent.fail_review = True
        base = self.repo.head()
        self.create_and_approve()
        self.engine.cycle()
        self.assertEqual(self.repo.head(), base)
        self.assertEqual(self.jira.blocked, ['APP-1'])
        self.assertTrue(list(self.repo.worktrees.glob('ticket-*')))

    def test_actionable_todo_blocks_creation_and_highest_approved_priority_wins(self):
        self.jira.add_ticket('Blocked', adf('Scope'), labels=['self-improver-blocked'])
        missing = self.jira.add_ticket('Needs scope', adf(''))
        first = self.jira.add_ticket('Lower priority', adf('Scope'), priority='2')
        second = self.jira.add_ticket('Higher priority', adf('Scope'))
        self.jira.approve(missing)
        _, _, waiting = self.engine.approved_ticket()
        self.assertIn(f'{missing} waiting for a description', waiting)
        self.assertEqual(self.jira.comment_items[missing], [])
        self.jira.approve(first)
        self.jira.approve(second)
        self.engine.cycle()
        self.assertEqual(self.jira.completed, [second])
        self.assertEqual(self.agent.calls, 0)

    def test_approval_during_recommendation_prevents_creation(self):
        key = self.jira.add_ticket('Manual ticket', adf('Scope'))
        recommend = self.agent.recommend

        def concurrent_approval(*args):
            self.jira.approve(key)
            return recommend(*args)

        with patch.object(self.agent, 'recommend', side_effect=concurrent_approval):
            self.engine.cycle()
        self.assertEqual([i['key'] for i in self.jira.items], [key])
        self.assertEqual(self.jira.completed, [key])
        self.assertEqual(self.state.get('recommendations'), {})

    def test_unapproved_todo_appearing_during_recommendation_allows_creation(self):
        recommend = self.agent.recommend
        def concurrent_todo(*args):
            self.jira.add_ticket('New manual ticket', adf('Scope'))
            return recommend(*args)
        with patch.object(self.agent, 'recommend', side_effect=concurrent_todo):
            self.engine.cycle()
        self.assertEqual([i['fields']['summary'] for i in self.jira.items], ['New manual ticket', 'First feature'])
        self.assertEqual([len(self.jira.comment_items[key]) for key in ('APP-1', 'APP-2')], [0, 0])
        self.assertEqual(self.jira.completed, [])

    def test_blocked_stale_and_ineligible_todo_allow_creation(self):
        blocked = self.jira.add_ticket('Approved then blocked', adf('Scope'))
        stale = self.jira.add_ticket('Changed scope', adf('Scope'))
        unmapped = self.jira.add_ticket('Unknown priority', adf('Scope'))
        foreign = self.jira.add_ticket('Other app', adf('Scope'))
        self.engine.approved_ticket()
        for key in (blocked, stale, unmapped, foreign):
            self.jira.approve(key)
        self.jira.issue(blocked)['fields']['labels'].append('self-improver-blocked')
        self.jira.record_change(stale, summary={})
        self.jira.issue(unmapped)['fields']['priority']['id'] = 'unmapped'
        self.jira.issue(foreign)['fields']['labels'].append('self-improver-app-other')

        self.engine.cycle()

        self.assertEqual(self.jira.items[-1]['fields']['summary'], 'First feature')
        self.assertEqual(len(self.jira.items), 5)
        self.assertEqual(self.jira.issue(stale)['fields']['status']['name'], 'Open')
        self.assertEqual(self.jira.comment_items[blocked], [])
        self.assertEqual(self.jira.completed, [])

    def test_failed_todo_search_prevents_creation(self):
        with patch.object(self.jira, 'ready', side_effect=[[], TransientError('Search unavailable')]):
            with self.assertRaisesRegex(TransientError, 'Search unavailable'):
                self.engine.cycle()
        self.assertEqual(self.jira.items, [])
        self.assertFalse(any(record['attempted'] for record in self.state.get('recommendations').values()))

    def test_multiple_recent_creates_are_checked_during_search_indexing_lag(self):
        manual = self.jira.add_ticket('Indexed manual ticket', adf('Scope'))
        with patch.object(self.jira, 'ready', return_value=[self.jira.issue(manual)]):
            self.engine.cycle()
            self.assertEqual(self.state.get('recent_jira_create'), ['APP-2'])
            self.state.set('recent_jira_create', 'APP-2')  # State written by older releases.
            self.engine.cycle()
            self.assertEqual(self.state.get('recent_jira_create'), ['APP-2', 'APP-3'])
            self.jira.approve('APP-2')
            self.engine.cycle()
            self.assertEqual(self.jira.completed, ['APP-2'])
        self.assertEqual(len(self.jira.items), 3)
        self.assertEqual(self.agent.calls, 2)
        self.assertEqual(self.jira.comment_items['APP-3'], [])
        self.engine.project_ready()
        self.assertEqual(self.state.get('recent_jira_create'), ['APP-3'])

    def test_failed_approval_lookup_prevents_creation(self):
        key = self.jira.add_ticket('Manual ticket', adf('Scope'))
        self.jira.approve(key)
        with patch.object(self.jira, 'changelog', side_effect=TransientError('History unavailable')):
            with self.assertRaisesRegex(TransientError, 'History unavailable'):
                self.engine.cycle()
        self.assertEqual([i['key'] for i in self.jira.items], [key])
        self.assertEqual(self.agent.calls, 0)

    def test_scope_edits_require_fresh_approval_even_when_reverted(self):
        for field in ('summary', 'description'):
            with self.subTest(field=field):
                key = self.jira.add_ticket(field, adf('First scope'))
                self.jira.approve(key)
                self.jira.record_change(key, **{field: {}})
                self.jira.record_change(key, **{field: {}})
                self.engine.cycle()
                self.assertEqual(self.jira.issue(key)['fields']['status']['name'], 'Open')
                self.assertNotIn(key, self.jira.completed)
                self.jira.approve(key)
                self.engine.cycle()
                self.assertIn(key, self.jira.completed)

    def test_scope_change_or_withdrawal_before_merge_preserves_worktree(self):
        base = self.repo.head()
        review = self.agent.review
        changes = {
            'edited': lambda key: self.jira.issue(key)['fields'].update(description=adf('New scope')),
            'withdrawn': lambda key: self.jira.transition(key, 'Open'),
            'moved': lambda key: self.jira.transition(key, 'Cancelled'),
            'deleted': lambda key: self.jira.items.remove(self.jira.issue(key)),
            'blocked': lambda key: self.jira.issue(key)['fields']['labels'].append('self-improver-blocked'),
        }
        for label, change in changes.items():
            with self.subTest(change=label):
                key = self.jira.add_ticket(label, adf('Approved scope'))
                self.engine.cycle()
                self.jira.approve(key)
                def review_and_change(*args):
                    change(key)
                    return review(*args)
                with patch.object(self.agent, 'review', side_effect=review_and_change):
                    self.engine.cycle()
                self.assertEqual(self.repo.head(), base)
                self.assertEqual(self.state.get('last_cycle')['outcome'], 'approval interrupted')
                self.assertTrue(list(self.repo.worktrees.glob(f'ticket-{key}-*')))
        self.assertFalse(self.jira.completed)

    def test_legacy_checkpoint_cannot_resume_implementation(self):
        self.state.set('cycle', {'phase': 'implement', 'key': 'APP-1'})
        with patch.object(self.agent, 'implement') as implement:
            self.engine.cycle()
            implement.assert_not_called()
        self.assertEqual(self.state.get('last_cycle')['outcome'], 'approval interrupted')

    def test_lost_comment_responses_reconcile_without_duplicates(self):
        self.create_and_approve()
        self.jira.lose_comment_kind = 'done'
        with self.assertRaises(TransientError):
            self.engine.cycle()
        merged = self.repo.head()
        self.assertEqual(self.jira.completed, [])
        self.engine.cycle()
        self.assertEqual(self.repo.head(), merged)
        self.assertEqual(self.jira.completed, ['APP-1'])
        comments = self.jira.comment_items['APP-1']
        self.assertEqual(len(comments), 1)
        self.assertIn('implemented', json.dumps(comments[-1]['body']))
        self.assertIn('test: passed', json.dumps(comments[-1]['body']))

    def test_unconfirmed_comment_waits_for_operator_before_retry(self):
        self.create_and_approve()
        with patch.object(self.jira, 'add_comment', side_effect=TransientError('Connection lost')) as post:
            with self.assertRaises(TransientError):
                self.engine.cycle()
            with self.assertRaisesRegex(TransientError, 'uncertain Jira comment'):
                self.engine.cycle()
            post.assert_called_once()
        operation, _ = self.state.pending()[0]
        (self.repo.home / 'config.json').write_text(json.dumps(asdict(self.cfg)), encoding='utf-8')
        args = ['--repo', str(self.root)]
        with redirect_stdout(io.StringIO()) as output, patch('improver.cli.configure_logging'):
            self.assertEqual(main([*args, 'status']), 0)
            self.assertIn(operation, json.loads(output.getvalue())['pending_jira_updates'][0]['operation_id'])
            self.assertEqual(main([*args, 'resolve-comment', operation, '--not-posted']), 0)
        self.engine.cycle()
        self.assertEqual(len(self.jira.comment_items['APP-1']), 1)
        self.assertFalse(self.state.pending())


if __name__ == '__main__':
    unittest.main()

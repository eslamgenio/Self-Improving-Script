import json
from pathlib import Path
import sys
import tempfile
import unittest

from improver.core import Config, Runner, State, TransientError
from improver.engine import Engine
from improver.git import Repository
from improver.jira import Jira
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

    def issues(self, extra=''):
        return [i for i in self.items if not extra or any(label in extra for label in i['fields']['labels'] if 'fp-' in label)]

    def create(self, rec, fp, base):
        key = f'APP-{len(self.items)+1}'
        self.items.append({'key': key, 'fields': {'summary': rec['title'], 'description': rec,
                           'priority': {'id': self.settings['priorities'][rec['priority_rank']]},
                           'created': key, 'labels': [f'self-improver-fp-{fp}'],
                           'status': {'name': 'Open', 'statusCategory': {'key': 'new'}}}})
        if self.fail_create:
            self.fail_create = False
            raise TransientError('Response lost after create')
        return key

    def issue(self, key):
        return next(i for i in self.items if i['key'] == key)

    def place_backlog(self, key):
        pass

    def transition(self, key, destination):
        self.issue(key)['fields']['status']['name'] = destination

    def complete(self, key, result):
        if self.fail_complete:
            self.fail_complete = False
            raise TransientError('offline')
        self.issue(key)['fields']['status']['statusCategory']['key'] = 'done'
        self.completed.append(key)

    def block(self, key, reason):
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
        return {'recommendations': [recommendation('First feature', 0), recommendation('Older feature', 1)] if self.calls == 1
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


class EngineTests(EngineCase):
    def test_two_cycles_review_updated_code_and_pick_older_priority(self):
        initial = self.repo.head()
        self.engine.cycle()
        first = self.repo.head()
        self.engine.cycle()
        self.assertNotEqual(initial, first)
        self.assertEqual(self.agent.contexts, [initial, first])
        self.assertEqual(self.jira.completed, ['APP-1', 'APP-2'])
        self.assertEqual(len(self.jira.items), 3)
        self.assertIsNone(self.state.get('cycle'))

    def test_uncertain_create_is_reconciled_without_duplicate(self):
        self.jira.fail_create = True
        with self.assertRaises(TransientError):
            self.engine.cycle()
        self.engine.cycle()
        self.assertEqual(len(self.jira.items), 2)
        self.assertEqual(self.jira.completed, ['APP-1'])

    def test_merged_change_is_not_reimplemented_when_jira_is_offline(self):
        self.jira.fail_complete = True
        with self.assertRaises(TransientError):
            self.engine.cycle()
        merged = self.repo.head()
        self.assertTrue(self.state.pending())
        self.engine.cycle()
        self.assertEqual(self.repo.head(), merged)
        self.assertEqual(self.jira.completed, ['APP-1'])

    def test_review_failure_blocks_without_merging(self):
        self.agent.fail_review = True
        base = self.repo.head()
        self.engine.cycle()
        self.assertEqual(self.repo.head(), base)
        self.assertEqual(self.jira.blocked, ['APP-1'])
        self.assertTrue(list(self.repo.worktrees.glob('ticket-*')))


if __name__ == '__main__':
    unittest.main()

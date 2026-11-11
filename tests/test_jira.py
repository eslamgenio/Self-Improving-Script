import unittest
from improver.core import Config, ConfigError, TransientError
from improver.jira import COMMENT_MARKER, Jira, adf, issue_description


class FakeTransport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        return next(self.responses)


class JiraTests(unittest.TestCase):
    def test_comment_marker_uses_a_json_object_and_recognizes_legacy_strings(self):
        transport = FakeTransport([{}, {'accountId': 'operator'}])
        client = Jira(Config(), transport=transport)
        marker = 'demo:approval:X-1:revision'
        client.add_comment('X-1', 'Approve the scope', marker)
        properties = transport.calls[0][2]['properties']
        self.assertEqual(properties, [{'key': COMMENT_MARKER, 'value': {'marker': marker}}])
        comment = {'author': {'accountId': 'operator'}, 'properties': properties}
        self.assertTrue(client.comment_exists([comment], marker))
        comment['properties'] = [{'key': COMMENT_MARKER, 'value': marker}]
        self.assertTrue(client.comment_exists([comment], marker))

    def test_todo_query_is_project_wide_even_for_manually_created_issues(self):
        transport = FakeTransport([{'issues': [{'key': 'X-1'}], 'isLast': True}])
        client = Jira(Config(jira={'project': 'X', 'statuses': {'backlog': 'To Do'}}), transport=transport)
        self.assertEqual(client.todo(), [{'key': 'X-1'}])
        self.assertEqual(transport.calls[0][2]['jql'], 'project = "X" AND (status = "To Do")')

    def test_changelog_pages_detect_scope_edits_without_confusing_status_changes(self):
        transport = FakeTransport([
            {'total': 2, 'values': [{'id': '1', 'items': [{'fieldId': 'description'}]}]},
            {'total': 2, 'values': [{'id': '2', 'items': [{'fieldId': 'status'}]}]},
        ])
        client = Jira(Config(), transport=transport)
        self.assertEqual(client.last_field_edit('X-1', {'summary', 'description'}), '1')
        self.assertIn('startAt=1', transport.calls[1][1])

    def test_search_paginates_and_is_application_scoped(self):
        transport = FakeTransport([{'issues': [{'key': 'X-1'}], 'nextPageToken': 'next'},
                                   {'issues': [{'key': 'X-2'}], 'isLast': True}])
        client = Jira(Config(app_id='demo', jira={'project': 'X'}), transport=transport)
        self.assertEqual([i['key'] for i in client.issues()], ['X-1', 'X-2'])
        self.assertIn('self-improver-app-demo', transport.calls[0][2]['jql'])
        self.assertEqual(transport.calls[1][2]['nextPageToken'], 'next')

    def test_selection_respects_jira_priority_and_age(self):
        client = Jira(Config(jira={'priorities': ['100', '7']}), transport=lambda *a: None)
        def issue(key, priority, age, labels=()):
            return {'key': key, 'fields': {'priority': {'id': priority}, 'created': age,
                    'labels': list(labels), 'status': {'statusCategory': {'key': 'new'}}}}
        items = [issue('X-1', '7', '2020'), issue('X-2', '100', '2022'),
                 issue('X-3', '100', '2021'), issue('X-4', '100', '2019', ['self-improver-blocked'])]
        self.assertEqual(client.select(items)['key'], 'X-3')

    def test_transition_resolves_current_transition_id(self):
        transport = FakeTransport([{'fields': {'status': {'name': 'Open'}}},
                                   {'transitions': [{'id': '42', 'to': {'name': 'Done'}, 'fields': {}}]}, {}])
        client = Jira(Config(), transport=transport)
        client.transition('X-1', 'Done')
        self.assertEqual(transport.calls[-1][2], {'transition': {'id': '42'}})

    def test_adf_preserves_multiline_text(self):
        doc = adf('a\nb')
        self.assertEqual(doc['type'], 'doc')
        self.assertEqual(len(doc['content']), 2)

    def test_creation_has_identity_priority_custom_fields_and_adf(self):
        cfg = Config(app_id='demo', jira={'project': 'X', 'priorities': ['1'],
                     'issue_types': {'feature': '10'}, 'custom_fields': {'customfield_1': 'team'}})
        transport = FakeTransport([{'key': 'X-1'}])
        client = Jira(cfg, transport=transport)
        rec = {'title': 'Search', 'kind': 'feature', 'priority_rank': 0, 'evidence': ['app.py:1']}
        client.create(rec, 'abc', 'commit')
        fields = transport.calls[0][2]['fields']
        self.assertEqual(fields['priority']['id'], '1')
        self.assertEqual(fields['customfield_1'], 'team')
        self.assertEqual(fields['description']['type'], 'doc')
        self.assertIn('self-improver-fp-abc', fields['labels'])


if __name__ == '__main__':
    unittest.main()

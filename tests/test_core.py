import tempfile
import unittest
from pathlib import Path

from improver.core import Config, State, RepoLock, ConfigError, recommendation_id


class CoreTests(unittest.TestCase):
    def test_state_is_durable(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'state.db'
            with State(path) as state:
                state.set('cycle', {'phase': 'merge', 'ticket': 'APP-1'})
                state.enqueue('done:APP-1', {'kind': 'done', 'key': 'APP-1'})
                state.enqueue('done:APP-1', {'kind': 'done', 'key': 'APP-1'})
            with State(path) as state:
                self.assertEqual(state.get('cycle')['phase'], 'merge')
                self.assertEqual(len(state.pending()), 1)

    def test_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as d:
            with RepoLock(Path(d) / 'lock'):
                with self.assertRaises(ConfigError):
                    with RepoLock(Path(d) / 'lock'):
                        pass
            with RepoLock(Path(d) / 'lock'):
                pass

    def test_fingerprint_normalizes_space_and_case(self):
        self.assertEqual(recommendation_id('Add Search', 'src/a.py'),
                         recommendation_id(' add  search ', 'SRC/a.py'))

    def test_config_rejects_credentials_and_bad_priorities(self):
        with self.assertRaises(ConfigError):
            Config.from_dict({'jira': {'token': 'secret'}})
        with self.assertRaises(ConfigError):
            Config.from_dict({'jira': {'priorities': []}})


if __name__ == '__main__':
    unittest.main()

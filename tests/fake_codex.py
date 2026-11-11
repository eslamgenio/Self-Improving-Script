"""Subprocess fixture implementing the Codex CLI protocol, without an AI call."""
import json
from pathlib import Path
import re
import subprocess
import sys


def recommendation(title, rank):
    return dict(title=title, category='Feature', kind='feature', priority_rank=rank, priority_reason='Fixture priority',
                business_value='Helpful user interaction', current_behavior='Interaction missing',
                desired_behavior='Interaction returns expected text', evidence=['app.py:1'],
                acceptance_criteria=['Interaction returns exact expected message'],
                implementation=['Add function and regression test'], skills=['test-driven-development'],
                effort='Small', dependencies=[], risks=['Low'], validation=['Unit test and runtime assertion'],
                migration_rollback='No migration; revert commit')


def checks(name=None):
    test = [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests']
    runtime = 'import app; assert app.VALUE == 1'
    if name:
        test += ['-p', f'test_{name}.py']
        runtime += f'; assert app.{name}("Eslam") == "{name}: Eslam"'
    return [{'name': 'tests', 'kind': 'test', 'argv': test},
            {'name': 'smoke', 'kind': 'runtime', 'argv': [sys.executable, '-c', runtime]}]


def main():
    args = sys.argv[1:]
    if '--version' in args:
        print('fake-codex 1.0')
        return
    if args[:2] == ['login', 'status']:
        print('Authenticated fixture')
        return
    prompt = sys.stdin.read()
    phase = next(p for p in ('discover', 'recommend', 'implement', 'review') if f'PHASE {p}.' in prompt)
    cwd = Path(args[args.index('-C') + 1])
    if phase == 'discover':
        result = {'setup': [], 'checks': checks(), 'reason': 'Fixture test/runtime commands'}
    elif phase == 'recommend':
        result = {'recommendations': [recommendation('Add greeting', 0)]
                  if 'def greeting' not in (cwd / 'app.py').read_text()
                  else [recommendation('Add greeting', 0), recommendation('Add shout', 2)], 'notes': 'fixture'}
    elif phase == 'implement':
        ticket = json.loads(prompt.split('\nTICKET ', 1)[1].split('\nCONTEXT ', 1)[0])
        name = ticket['fields']['summary'].split()[-1]
        test_path = cwd / 'tests' / f'test_{name}.py'
        test_path.write_text(f'''import unittest
import app
class FeatureTest(unittest.TestCase):
    def test_interaction(self):
        self.assertEqual(app.{name}('Eslam'), '{name}: Eslam')
''', encoding='utf-8')
        red = subprocess.run(checks(name)[0]['argv'], cwd=cwd, capture_output=True)
        if f'def {name}' not in (cwd / 'app.py').read_text():
            assert red.returncode != 0, 'Regression must fail before implementation'
            with (cwd / 'app.py').open('a', encoding='utf-8') as stream:
                stream.write(f'\ndef {name}(name):\n    return f"{name}: {{name}}"\n')
        with (cwd / 'README.md').open('a', encoding='utf-8') as stream:
            stream.write(f'\n{name}(name) returns a user interaction.\n')
        print(json.dumps({'red_test_exit': red.returncode}))
        result = {'blocked': False, 'reason': '', 'summary': f'Add {name}',
                  'acceptance_evidence': ['Runtime assertion and regression test cover the exact response'],
                  'documentation': ['README.md'], 'tests_added': [f'tests/test_{name}.py'], 'ticket_checks': checks(name)}
    else:
        match = re.search(r'Inspect git diff ([a-f0-9]+)\.\.([a-f0-9]+)', prompt)
        diff = subprocess.run(['git', 'diff', '--check', f'{match[1]}..{match[2]}'], cwd=cwd, capture_output=True)
        result = {'approved': diff.returncode == 0, 'findings': [], 'reason': 'Fixture independent diff check'}
    output = Path(args[args.index('-o') + 1])
    output.write_text(json.dumps(result), encoding='utf-8')
    print(json.dumps({'type': 'turn.completed', 'phase': phase}))


if __name__ == '__main__':
    main()

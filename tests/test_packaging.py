from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


class PackagingTests(unittest.TestCase):
    def test_offline_wheel_contains_package_and_installed_cli_runs(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='packaging ') as d:
            root = Path(d)
            project = root / 'source'
            project.mkdir()
            shutil.copy2(source / 'pyproject.toml', project)
            shutil.copytree(source / 'improver', project / 'improver', ignore=shutil.ignore_patterns('__pycache__'))
            result = subprocess.run([sys.executable, '-m', 'pip', 'wheel', str(project), '--no-deps',
                                     '--no-build-isolation', '--no-index', '--wheel-dir', str(root / 'wheels')],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            wheel = next((root / 'wheels').glob('*.whl'))
            with zipfile.ZipFile(wheel) as archive:
                self.assertIn('improver/engine.py', archive.namelist())
            target = root / 'installed'
            result = subprocess.run([sys.executable, '-m', 'pip', 'install', str(wheel), '--no-deps',
                                     '--no-index', '--target', str(target)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([sys.executable, '-I', '-c',
                'import sys; sys.path.insert(0,sys.argv[1]); from improver.cli import main; main(["--help"])', str(target)],
                cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('resolve-create', result.stdout)


if __name__ == '__main__':
    unittest.main()

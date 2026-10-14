from pathlib import Path
import subprocess
import tempfile
import unittest
from code_evidence import collect_code


class CodeEvidenceTests(unittest.TestCase):
    def test_only_registered_source_diff_with_bounded_untracked_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
            git('init', '-q')
            git('config', 'user.email', 'test@example.invalid')
            git('config', 'user.name', 'Test')
            (root/'src').mkdir()
            (root/'src/a.py').write_text('original = 1\n')
            git('add', 'src/a.py')
            git('commit', '-qm', 'fixture')
            (root/'src/a.py').write_text('changed = 2\n')
            (root/'src/new.py').write_text('x = 1\n'*1000)
            (root/'src/data').mkdir()
            (root/'src/data/private.py').write_text('must not be included')
            (root/'outside.py').write_text('unregistered')
            result = collect_code(root, [{'id': 'test', 'review_code_paths': ['src']}])
            entries = {item['path']: item for item in result['entries']}
            self.assertEqual(set(entries), {'src/a.py', 'src/new.py'})
            self.assertIn('+changed = 2', entries['src/a.py']['content'])
            self.assertTrue(entries['src/new.py']['truncated'])
            self.assertLessEqual(len(entries['src/new.py']['content']), 3000)
            self.assertEqual((root/'src/a.py').read_text(), 'changed = 2\n')

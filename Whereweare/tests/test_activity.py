import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from activity import activity_context
from hub import Hub
from review_store import ReviewStore


class ActivityTests(unittest.TestCase):
    def test_git_and_mtime_are_hints_without_activating_or_publishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root/'work').mkdir()
            (root/'work'/'STATUS.md').write_text('# 仅整理文档\n')
            subprocess.run(['git', '-C', str(root), 'add', 'work'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c',
                            'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], check=True)
            (root/'work'/'STATUS.md').write_text('# 新文字，不是新的成果日期\n')
            (root/'work'/'new-code.py').write_text('PRIVATE_CODE_MUST_NOT_BE_READ = 1\n')
            config = root/'registry.json'
            config.write_text(json.dumps({'projects': [{'id': 'work', 'name': 'Work',
                'source_paths': ['work/STATUS.md'], 'activity_paths': ['work']}]}))
            hub = Hub(root, root/'runtime', config)
            hub.update_tracking('work', {'activity': 'idea'})
            store = ReviewStore(hub)
            packet = store.prepare()
            hints = packet['activity_context'][0]
            self.assertTrue(hints['hint_only'])
            self.assertTrue(hints['repositories'][0]['last_commit']['sha'])
            self.assertTrue(hints['repositories'][0]['changes'])
            self.assertIn('modified_at', hints['documents'][0])
            self.assertNotIn('PRIVATE_CODE_MUST_NOT_BE_READ', json.dumps(packet))
            self.assertEqual(hub.tracking_preferences()['work']['activity'], 'idea')
            self.assertFalse(store.current_path.exists())

    def test_missing_git_and_paths_outside_workspace_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = activity_context(Path(tmp), [{'id': 'unknown', 'activity_paths': ['../', 'missing'],
                                                'source_paths': ['../secret.md']}])
            self.assertEqual(rows[0]['repositories'], [])
            self.assertEqual(rows[0]['documents'], [])

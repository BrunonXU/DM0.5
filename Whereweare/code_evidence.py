"""Bounded diffs for explicitly registered source directories, only during review."""
from pathlib import Path
import hashlib
import subprocess

EXTENSIONS = {'.py', '.js', '.ts', '.tsx', '.jsx', '.css', '.html', '.sh', '.cpp', '.h', '.hpp'}
EXCLUDED = {'node_modules', 'vendor', 'third_party', 'data', 'datasets', 'models', 'weights', 'runtime', '.git', '.venv', 'artifacts', 'results'}


def collect_code(workspace, projects, previous=None):
    workspace = Path(workspace).resolve()
    previous = previous or {}
    entries, heads, budget = [], {}, 16000
    def git(root, *args):
        try:
            p = subprocess.run(['git', '--no-optional-locks', '-C', str(root), *args], capture_output=True, text=True, timeout=5)
            return p.stdout if p.returncode == 0 else ''
        except (OSError, subprocess.TimeoutExpired):
            return ''
    for project in projects:
        for relative in project.get('review_code_paths', []):
            scope = workspace / relative
            if not scope.exists() or scope.is_symlink() or not scope.resolve().is_relative_to(workspace):
                continue
            root_name = git(scope if scope.is_dir() else scope.parent, 'rev-parse', '--show-toplevel').strip()
            if not root_name:
                continue
            root = Path(root_name)
            if not root.is_relative_to(workspace):
                continue
            key = str(root.relative_to(workspace))
            head = git(root, 'rev-parse', 'HEAD').strip()
            base = previous.get(key) or head
            heads[key] = head
            pathspec = str(scope.relative_to(root))
            changed = git(root, 'diff', '--name-only', '-z', base, '--', pathspec).split('\0')
            untracked = git(root, 'ls-files', '--others', '--exclude-standard', '-z', '--', pathspec).split('\0')
            paths = [p for p in dict.fromkeys(changed + untracked) if p and Path(p).suffix in EXTENSIONS and not EXCLUDED.intersection(Path(p).parts)]
            for name in paths:
                if len(entries) >= 12 or budget <= 0:
                    entries.append({'project_id': project['id'], 'pending': True, 'reason': 'code_budget'})
                    break
                file = root / name
                if file.is_symlink() or not file.resolve().is_relative_to(workspace):
                    continue
                content = b''
                if file.exists():
                    if file.stat().st_size > 250000:
                        entries.append({'project_id': project['id'], 'path': str(file.relative_to(workspace)), 'pending': True, 'reason': 'large_file'})
                        continue
                    content = file.read_bytes()
                patch = git(root, 'diff', '--no-ext-diff', '--no-textconv', '--unified=3', base, '--', name) if name not in untracked else content.decode('utf-8', errors='replace')
                limit = min(3000, budget)
                entries.append({'project_id': project['id'], 'path': str(file.relative_to(workspace)),
                                'sha256': hashlib.sha256(content).hexdigest() if file.exists() else None,
                                'kind': 'untracked_excerpt' if name in untracked else 'diff', 'content': patch[:limit],
                                'truncated': len(patch) > limit, 'hint_only': True})
                budget -= min(len(patch), limit)
    return {'entries': entries, 'observed_heads': heads, 'note': '差异只说明实现变化，不证明运行、测试或部署。截断不计作完整代码审阅。'}

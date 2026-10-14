"""Bounded local activity hints, collected only when preparing a review.

No code contents, recursive mtime walks, or robot access. These timestamps must
never be used as event dates or to change the user's tracking preferences.
"""
import datetime as dt
from pathlib import Path
import subprocess


def activity_context(workspace, projects):
    workspace = Path(workspace).resolve()
    results = []
    cache = {}

    def git(cwd, *args):
        key = (str(cwd), args)
        if key not in cache:
            try:
                run = subprocess.run(['git', '--no-optional-locks', '-C', str(cwd), *args],
                                     capture_output=True, text=True, timeout=4, check=False)
                cache[key] = run.stdout if run.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired):
                cache[key] = None
        return cache[key]

    for project in projects:
        entry = {'project_id': project['id'], 'hint_only': True, 'repositories': [], 'documents': []}
        scopes = {}
        for relative in project.get('activity_paths', [])[:8]:
            path = (workspace / relative).resolve()
            if not path.is_relative_to(workspace) or not path.exists():
                continue
            root_text = git(path if path.is_dir() else path.parent, 'rev-parse', '--show-toplevel')
            if not root_text:
                continue
            root = Path(root_text.strip()).resolve()
            if not root.is_relative_to(workspace):
                continue
            scopes.setdefault(root, []).append(str(path.relative_to(root)))
        for root, paths in scopes.items():
            commit = git(root, 'log', '-1', '--format=%H%n%cI', '--', *paths)
            dirty = git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=normal', '--', *paths)
            # NUL separation keeps spaces/newlines in filenames intact. Rename
            # records have an extra source path; retain both as diagnostic hints.
            records = (dirty or '').split('\0')
            records = [item for item in records if item]
            parts = (commit or '').splitlines()
            entry['repositories'].append({'root': str(root.relative_to(workspace)), 'paths': paths,
                'last_commit': {'sha': parts[0], 'committed_at': parts[1]} if len(parts) >= 2 else None,
                'changes': records[:40], 'changes_truncated': len(records) > 40,
                'available': dirty is not None})
        for relative in project.get('source_paths', [])[:12]:
            path = (workspace / relative).resolve()
            if not path.is_relative_to(workspace) or not path.is_file():
                continue
            entry['documents'].append({'path': relative,
                'modified_at': dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).isoformat()})
        results.append(entry)
    return results

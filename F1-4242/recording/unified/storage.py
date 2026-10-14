"""Local-first storage plan. Import/read never creates directories or transfers data."""
from pathlib import Path

from .contract import confined

SCHEMA = 'f1_local_storage_v1'
SOURCES = {
    'pico': 'pico_data',
    'demonstration': 'hil_data/demonstration',
    'policy_hil': 'hil_data/policy_hil',
    'datasets': 'datasets',
}


def layout(root):
    root = Path(root).expanduser()
    if not root.is_absolute():
        raise ValueError('data root must be absolute')
    confined(root, '.')
    return {name: str(confined(root, relative)) for name, relative in SOURCES.items()}


def read_config(path):
    import json
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError('invalid storage configuration file')
    value = json.loads(path.read_text())
    if value.get('schema') != SCHEMA:
        raise ValueError('unsupported storage configuration')
    paths = layout(value['root'])
    archive = value.get('archive', {})
    # This module describes destinations only. It never starts an uploader.
    if archive.get('enabled', False):
        raise ValueError('archive activation requires a separately configured and verified worker')
    legacy = value.get('legacy_roots', {})
    if not isinstance(legacy, dict):
        raise ValueError('legacy roots must be a mapping')
    for name, source in legacy.items():
        if name not in SOURCES or not isinstance(source, list):
            raise ValueError('invalid legacy source group')
        for item in source:
            layout(item)
    return dict(value, paths=paths)


def migration_inventory(config):
    """Immediate children only; no raw reads, moves, hashes or automated approvals.

    Destination collisions are reported, never merged. Symlinks are reported but
    never followed. A later migration must verify full manifests and source IDs.
    """
    rows = []
    for kind, roots in config.get('legacy_roots', {}).items():
        destination = Path(config['paths'][kind])
        for root_value in roots:
            root = Path(root_value).expanduser()
            if not root.exists():
                rows.append(dict(source=str(root), kind=kind, state='missing'))
                continue
            if not root.is_dir():
                raise ValueError('legacy root is not a directory')
            confined(root, '.')
            for index, item in enumerate(sorted(root.iterdir())):
                if index >= 10000:
                    raise ValueError('legacy inventory too large; narrow the configured root')
                target = destination/item.name
                state = ('symlink_skipped' if item.is_symlink() else
                         'already_in_place' if item == target else
                         'destination_exists' if target.exists() or target.is_symlink() else
                         'review_required')
                rows.append(dict(source=str(item), destination=str(target), kind=kind,
                                 entry_type='directory' if not item.is_symlink() and item.is_dir() else 'file',
                                 state=state))
    return dict(schema='f1_storage_inventory_v1', root=config['root'], entries=rows,
                executed=False, raw_modified=False, uploads_started=False)


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description='Read-only F1 local storage and migration inventory')
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = read_config(args.config)
    print(json.dumps(migration_inventory(config), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

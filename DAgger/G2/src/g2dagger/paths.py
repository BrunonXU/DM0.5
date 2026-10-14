"""Local artifact paths for the workspace and existing flat G2 deployments."""
import os
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parents[2]


def artifact_root(platform_root=None):
    """Keep legacy flat deployments stable; consolidate workspace outputs."""
    configured = os.environ.get("DAGGER_ARTIFACTS_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    root = Path(platform_root) if platform_root is not None else PLATFORM_ROOT
    if (root.parent / "shared/src/dagger_core").is_dir():
        return root.parent / "artifacts"
    return root

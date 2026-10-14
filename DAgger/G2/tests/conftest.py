from pathlib import Path
import os
import sys
root=Path(__file__).resolve().parents[2]
for path in (root/"F1/src",root/"G2/src",root/"shared/src",root/"F1/tests",root/"G2/tests"):
    if path.exists():sys.path.insert(0,str(path))

# The real-controller compatibility test uses a separate, optional checkout.
client_root = Path(os.environ.get("G2_PROJECT_DIR", root.parent / "g2_robot")).expanduser()
if (client_root / "g2_sort").is_dir():
    sys.path.insert(0, str(client_root))

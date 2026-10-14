from pathlib import Path
import sys
root=Path(__file__).resolve().parents[2]
for path in (root/"F1/src",root/"G2/src",root/"shared/src",root/"F1/tests",root/"G2/tests"):
    if path.exists():sys.path.insert(0,str(path))

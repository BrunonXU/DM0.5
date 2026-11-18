"""Private SSH-to-Unix web-control transport; never exposes authorization to planner RPC."""
import hashlib
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parent.parent
manifest = json.loads((root / 'manifest.json').read_text())
for relative in ('native/owner_request.py', 'src/f1_harness/ipc.py', 'src/f1_harness/owner_transport_server.py'):
    path = root / relative
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != manifest['files'][relative]:
        raise ValueError('private_transport_source_changed')
sys.path[:0] = [str(root / 'deps'), str(root / 'src')]
from f1_harness.ipc import UnixClient

site = Path(sys.argv[1])
if not site.is_absolute() or not site.is_relative_to('/home/user/Bruno/HARNESS/site'):
    raise ValueError('registered_site_required')
config = json.loads((site / 'owner.local.json').read_text())
client = UnixClient(Path(config['owner_socket']), timeout=3)
streaming = sys.argv[2:] == ['--stream']
if sys.argv[2:] and not streaming:
    raise ValueError('unknown_private_transport_option')

from f1_harness.owner_transport_server import serve
raise SystemExit(serve(client, sys.stdin.buffer, sys.stdout, sys.stderr, streaming=streaming))

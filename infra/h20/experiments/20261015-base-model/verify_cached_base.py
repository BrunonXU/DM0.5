#!/usr/bin/env python3
"""Read-only verification of an existing GCS pi05_base cache.

Requires google-crc32c for upstream CRC32C validation; hashes every object with
SHA256 as a portable transfer manifest. No model file is changed.
"""
import argparse
import base64
import datetime
import hashlib
import json
from pathlib import Path

import google_crc32c


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--objects", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.objects.read_text())
    assert not manifest.get("nextPageToken"), "Incomplete paginated manifest"
    files = []
    for obj in sorted(manifest["items"], key=lambda x: x["name"]):
        prefix = "checkpoints/pi05_base/"
        assert obj["name"].startswith(prefix)
        rel = obj["name"][len(prefix):]
        assert not Path(rel).is_absolute() and ".." not in Path(rel).parts
        path = args.source / rel
        assert path.is_file() and not path.is_symlink(), path
        assert path.stat().st_size == int(obj["size"]), path
        sha = hashlib.sha256()
        md5 = hashlib.md5()
        crc = google_crc32c.Checksum()
        with path.open("rb") as stream:
            while chunk := stream.read(8 * 1024 * 1024):
                sha.update(chunk)
                md5.update(chunk)
                crc.update(chunk)
        assert base64.b64encode(crc.digest()).decode() == obj["crc32c"], path
        if obj.get("md5Hash"):
            assert base64.b64encode(md5.digest()).decode() == obj["md5Hash"], path
        record = dict(path=rel, size=path.stat().st_size, sha256=sha.hexdigest(),
                      gcs_generation=obj["generation"], gcs_crc32c=obj["crc32c"],
                      upstream_checks="CRC32C + MD5" if obj.get("md5Hash") else "CRC32C")
        files.append(record)
        print("PASS", rel, flush=True)
    revision = hashlib.sha256(json.dumps(
        [{k: item[k] for k in ("path", "size", "gcs_generation", "gcs_crc32c")} for item in files],
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result = dict(result="PASS", checked_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  source=str(args.source.resolve()), official_source="gs://openpi-assets/checkpoints/pi05_base",
                  format="JAX Orbax OCDBT", revision_manifest_sha256=revision,
                  total_bytes=sum(x["size"] for x in files), files=files)
    with args.output.open("x") as out:
        json.dump(result, out, indent=2)
        out.write("\n")
    print("VERIFIED", len(files), "files", result["total_bytes"], "bytes", "revision", revision)


if __name__ == "__main__":
    main()

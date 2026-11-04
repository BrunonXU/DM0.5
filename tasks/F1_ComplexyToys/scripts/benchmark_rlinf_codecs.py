"""CPU-only, historical-image PNG1/RLinf codec benchmark; no robot imports.

The fixed upstream backend factory is executed unchanged. A NumPy transport
adapter replaces its Torch-only tensor wrapper for the existing F1 CPU client.
Each camera is one [1,H,W,C] image; no temporal/cross-camera XOR is applied.
Missing python-zstandard can use the installed libzstd through an explicit
ctypes adapter. No packages, services, model inference, or GPU work are needed.
"""
from __future__ import annotations

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import ctypes
import ctypes.util
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import importlib.metadata
import importlib.util
from io import BytesIO
import json
import os
from pathlib import Path
import platform
import random
import sys
import time
import types
from typing import Any, Callable

import numpy as np
from PIL import Image

CAMERAS = ("head", "left_wrist", "right_wrist")
CODECS = ("png1", "lz4_1", "zstd_1", "zstd_3")
MASKED_CODEC = "png1_masked_left_blank"


def digest(value):
    return hashlib.sha256(value).hexdigest()


def load_module(path):
    spec = importlib.util.spec_from_file_location("codec_frozen_processing", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def masked_left_png(contract):
    """Explicit right-only experiment; reject every legacy three-active view model."""
    if (contract.get("input_version") != "right_only_v1"
            or contract.get("active_cameras") != ["head", "right_wrist"]
            or contract.get("cameras") != list(CAMERAS)
            or contract.get("image_size") != 224):
        raise ValueError("Masked left transport requires the exact right_only_v1 input contract")
    output = BytesIO()
    Image.fromarray(np.zeros((224, 224, 3), dtype=np.uint8)).save(output, format="PNG", compress_level=1)
    return output.getvalue()


def zstd_fallback():
    """Provide the two upstream Python API calls with the system libzstd."""
    library_path = ctypes.util.find_library("zstd")
    if not library_path:
        raise RuntimeError("Neither python-zstandard nor system libzstd is available")
    lib = ctypes.CDLL(library_path)
    size = ctypes.c_size_t
    pointer = ctypes.c_void_p
    for name, args, result in (
        ("ZSTD_compressBound", [size], size),
        ("ZSTD_compress", [pointer, size, pointer, size, ctypes.c_int], size),
        ("ZSTD_getFrameContentSize", [pointer, size], ctypes.c_ulonglong),
        ("ZSTD_decompress", [pointer, size, pointer, size], size),
        ("ZSTD_isError", [size], ctypes.c_uint),
        ("ZSTD_getErrorName", [size], ctypes.c_char_p),
        ("ZSTD_versionString", [], ctypes.c_char_p),
    ):
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, result

    def checked(result):
        if lib.ZSTD_isError(result):
            raise RuntimeError(lib.ZSTD_getErrorName(result).decode())
        return result

    class Compressor:
        def __init__(self, level):
            self.level = level

        def compress(self, raw):
            output = ctypes.create_string_buffer(lib.ZSTD_compressBound(len(raw)))
            length = checked(lib.ZSTD_compress(output, len(output), raw, len(raw), self.level))
            return output.raw[:length]

    class Decompressor:
        def decompress(self, raw):
            length = lib.ZSTD_getFrameContentSize(raw, len(raw))
            if length >= 2**64 - 2 or length > 16 * 1024 * 1024:
                raise ValueError("Invalid or oversized frame content size")
            output = ctypes.create_string_buffer(length)
            actual = checked(lib.ZSTD_decompress(output, length, raw, len(raw)))
            if actual != length:
                raise ValueError("Zstd decompressed size mismatch")
            return output.raw[:actual]

    module = types.ModuleType("zstandard")
    module.ZstdCompressor = Compressor
    module.ZstdDecompressor = Decompressor
    sys.modules["zstandard"] = module
    return {"kind": "system_libzstd_ctypes", "library": library_path,
            "version": lib.ZSTD_versionString().decode(),
            "limitation": "same codec algorithm; Python binding/allocation differs from upstream"}


def backend_factory(path):
    """Execute only the fixed backend factory, without importing unused Torch."""
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name == "_get_backend"]
    if len(selected) != 1:
        raise ValueError("Expected one fixed upstream backend factory")
    namespace = {"lru_cache": lru_cache, "Any": Any, "Callable": Callable,
                 "_INSTALL_HINT": {"lz4": "pip install lz4", "zstd": "pip install zstandard"}}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    if importlib.util.find_spec("zstandard"):
        zstd = {"kind": "python_zstandard", "version": importlib.metadata.version("zstandard")}
    else:
        zstd = zstd_fallback()
    return namespace["_get_backend"], zstd


def read_inputs(paths):
    samples, seen = [], set()
    for index in paths:
        if index.suffix == ".jsonl":
            rows = [json.loads(line) for line in index.read_text().splitlines() if line]
        else:
            value = json.loads(index.read_text())
            rows = list(value.values()) if isinstance(value, dict) else value
        for row in rows:
            if set(row["images"]) != set(CAMERAS):
                continue
            hashes = row.get("image_sha256", row.get("sha256"))
            raw = tuple((index.parent / row["images"][camera]).read_bytes() for camera in CAMERAS)
            for camera, payload in zip(CAMERAS, raw):
                if digest(payload) != hashes[camera]:
                    raise ValueError(f"Historical image hash mismatch: {index}, {camera}")
            identity = tuple(digest(value) for value in raw)
            if identity in seen:
                continue
            seen.add(identity)
            samples.append({"id": row.get("id", str(row.get("request_id", row.get("observation_id")))),
                            "source_index": str(index), "episode": row.get("episode"),
                            "raw": raw, "raw_sha256": dict(zip(CAMERAS, identity))})
    if not samples:
        raise ValueError("No complete three-camera historical observations")
    return samples


def quantiles(values):
    return dict(zip(("p50", "p95", "p99", "max"),
                    np.percentile(values, (50, 95, 99, 100)).tolist()))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", type=Path, action="append", required=True)
    p.add_argument("--assets", type=Path, required=True)
    p.add_argument("--upstream", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--rounds", type=int, default=240)
    p.add_argument("--warmup", type=int, default=12)
    p.add_argument("--reference", type=Path)
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--codecs", nargs="+", choices=(*CODECS, MASKED_CODEC), default=list(CODECS))
    args = p.parse_args()
    if args.rounds < 200 or args.workers != 3:
        p.error("Use at least 200 paired rounds and the current three image workers")
    args.output.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    pillow_enum_compat = not hasattr(Image, "Resampling")
    if pillow_enum_compat:
        # Pillow <9.1 spells the same bilinear constant at module scope.
        # Cross-host exact pixel hashes remain mandatory for ARM comparison.
        Image.Resampling = types.SimpleNamespace(BILINEAR=Image.BILINEAR)
    processing = load_module(args.assets / "processing.py")
    contract = processing.load_contract(args.assets / "contract.json")
    if contract.get("input_version") != "right_only_v1":
        raise ValueError("This comparison requires the current right-only processor")
    masked_marker = {"codec": "png1", "data": masked_left_png(contract)} if MASKED_CODEC in args.codecs else None
    get_backend, zstd = backend_factory(args.upstream)
    samples = read_inputs(args.inputs)

    def prepare(raw, camera):
        with Image.open(BytesIO(raw)) as image:
            return processing.process_image(image, camera, contract)

    for sample in samples:
        sample["pixels"] = tuple(prepare(raw, camera) for camera, raw in zip(CAMERAS, sample["raw"]))
        sample["pixels_sha256"] = dict(zip(CAMERAS, (digest(x.tobytes()) for x in sample["pixels"])))
    pixel_references = [dict(id=s["id"], raw_sha256=s["raw_sha256"], pixels_sha256=s["pixels_sha256"])
                        for s in samples]
    if args.reference and pixel_references != json.loads(args.reference.read_text()):
        raise ValueError("Cross-host original/processed pixel identity differs")
    (args.output / "pixel-reference.json").write_text(json.dumps(pixel_references, indent=2))

    def encode(pixels, codec):
        if codec == "png1":
            output = BytesIO()
            Image.fromarray(pixels).save(output, format="PNG", compress_level=1)
            return {"codec": "png1", "data": output.getvalue()}
        name, level = codec.split("_")
        compress, _ = get_backend(name)
        return {"__obs_codec__": name, "shape": [1, *pixels.shape], "dtype": "uint8",
                "device": "cpu", "xor": False,
                "data": compress(np.ascontiguousarray(pixels).tobytes(), int(level))}

    def decode(marker):
        if marker.get("codec") == "png1":
            with Image.open(BytesIO(marker["data"])) as image:
                return np.asarray(image.convert("RGB")).copy()
        _, decompress = get_backend(marker["__obs_codec__"])
        if marker["xor"] or marker["shape"][0] != 1:
            raise ValueError("Temporal/cross-camera XOR is prohibited for this comparison")
        return np.frombuffer(decompress(marker["data"]), dtype=np.uint8).reshape(marker["shape"])[0].copy()

    def worker(raw, pixels, camera, codec, mode):
        cpu_start = time.thread_time_ns()
        if codec == MASKED_CODEC and camera == "left_wrist":
            return masked_marker, 0., 0., (time.thread_time_ns()-cpu_start)/1e6
        if codec == MASKED_CODEC:
            codec = "png1"
        prep_start = time.perf_counter_ns()
        if mode == "original_to_payload":
            pixels = prepare(raw, camera)
        prep_end = time.perf_counter_ns()
        marker = encode(pixels, codec)
        end = time.perf_counter_ns()
        return marker, (prep_end-prep_start)/1e6, (end-prep_end)/1e6, (time.thread_time_ns()-cpu_start)/1e6

    rows = []
    rng = random.Random(20261023)
    order = list(range(len(samples)))
    rng.shuffle(order)
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="codec-offline") as pool:
        for round_number in range(-args.warmup, args.rounds):
            sample_number = order[round_number % len(samples)]
            sample = samples[sample_number]
            choices = [(mode, codec) for mode in ("prepared_pixels", "original_to_payload") for codec in args.codecs]
            rng.shuffle(choices)
            for mode, codec in choices:
                begin_cpu, begin = time.process_time_ns(), time.perf_counter_ns()
                futures = [pool.submit(worker, raw, pixels, camera, codec, mode)
                           for raw, pixels, camera in zip(sample["raw"], sample["pixels"], CAMERAS)]
                results = [future.result() for future in futures]
                encoded = time.perf_counter_ns()
                restored = [decode(result[0]) for result in results]
                end, end_cpu = time.perf_counter_ns(), time.process_time_ns()
                expected = tuple(np.zeros_like(want) if codec == MASKED_CODEC and camera == "left_wrist" else want
                                 for camera, want in zip(CAMERAS, sample["pixels"]))
                exact = all(np.array_equal(want, got) for want, got in zip(expected, restored))
                if not exact:
                    raise ValueError("Lossless roundtrip failed")
                if round_number < 0:
                    continue
                payload_bytes = sum(len(result[0]["data"]) for result in results)
                marker_bytes = sum(len(json.dumps({k: v for k, v in result[0].items() if k != "data"},
                                                  separators=(",", ":")).encode()) for result in results)
                rows.append(dict(round=round_number, sample_number=sample_number, sample_id=sample["id"],
                                 mode=mode, codec=codec, exact=True, image_payload_bytes=payload_bytes,
                                 codec_marker_json_bytes=marker_bytes,
                                 encode_wall_ms=(encoded-begin)/1e6, decode_wall_ms=(end-encoded)/1e6,
                                 total_wall_ms=(end-begin)/1e6, total_cpu_ms=(end_cpu-begin_cpu)/1e6,
                                 image_prepare_sum_ms=sum(result[1] for result in results),
                                 image_encode_sum_ms=sum(result[2] for result in results),
                                 encode_workers_cpu_ms=sum(result[3] for result in results)))
            if (round_number+1) % 60 == 0:
                print(json.dumps({"completed_paired_rounds": round_number+1}), flush=True)
    summary = {"schema": "f1_rlinf_lossless_codecs_offline_v1", "started_at_utc": started,
               "finished_at_utc": datetime.now(timezone.utc).isoformat(),
               "machine": platform.node(), "platform": platform.platform(), "python": sys.version,
               "cpu_count": os.cpu_count(), "loadavg": os.getloadavg(),
               "numpy": np.__version__, "pillow": importlib.metadata.version("Pillow"),
               "pillow_enum_compat": pillow_enum_compat,
               "lz4": importlib.metadata.version("lz4"), "zstd_backend": zstd,
               "script_sha256": digest(Path(__file__).read_bytes()),
               "upstream_sha256": digest(args.upstream.read_bytes()),
               "processing_sha256": digest((args.assets/"processing.py").read_bytes()),
               "contract_sha256": digest((args.assets/"contract.json").read_bytes()),
               "paired_rounds_per_mode": args.rounds, "warmup_rounds_excluded": args.warmup,
               "unique_observations": len(samples), "unique_images": len({h for s in samples for h in s["raw_sha256"].values()}),
               "cohorts": {str(index): sum(s["source_index"] == str(index) for s in samples) for index in args.inputs},
               "cameras": list(CAMERAS), "active_model_cameras": contract["active_cameras"],
               "workers": 3, "decode_workers": 1, "xor": False, "batch_per_camera": 1,
               "codecs": args.codecs,
               "masked_left_constant_bytes": len(masked_marker["data"]) if masked_marker else None,
               "cross_host_pixels_exact": bool(args.reference), "exact_roundtrips": len(rows),
               "robot_commands": 0, "gpu_used": False,
               "limits": ["Repeated timing rounds are not independent scene samples.",
                          "Historical images span listed cohorts; no current camera capture or robot action.",
                          "Current three-camera payload retained, including masked left wrist.",
                          "NumPy adapter uses fixed upstream backend; excludes Torch wrapper overhead.",
                          "Image payload bytes exclude common RTC metadata and transport overhead.",
                          "CPU wall times are local codec measurements, not measured network RTT or closed-loop latency."],
               "results": {}}
    numeric = [key for key in rows[0] if key.endswith("_ms") or key.endswith("_bytes")]
    for mode in ("prepared_pixels", "original_to_payload"):
        summary["results"][mode] = {}
        for codec in args.codecs:
            selection = [row for row in rows if row["mode"] == mode and row["codec"] == codec]
            summary["results"][mode][codec] = {key: quantiles([row[key] for row in selection]) for key in numeric}
    if masked_marker:
        summary["limits"].append("Masked-left candidate preserves head/right pixels and three slots; left transport pixels intentionally become constant black. Complete model input equivalence is a separate check; legacy Full must reject this mode.")
    (args.output/"measurements.jsonl").write_text("".join(json.dumps(row)+"\n" for row in rows))
    (args.output/"summary.json").write_text(json.dumps(summary, indent=2)+"\n")
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()

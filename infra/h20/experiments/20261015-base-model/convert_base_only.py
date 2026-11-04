#!/usr/bin/env python3
"""Use pinned RLinf transforms without attaching another robot's norm stats.

CPU only. Input is a checksum-verified official pi05_base Orbax checkpoint;
output is a new bare OpenPI_RLinf fp32 model plus provenance. This prepares
weights, not an approved F1 dataset/configuration or trained checkpoint.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import sys

os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["JAX_PLATFORMS"] = "cpu"
os.environ.setdefault("OMP_NUM_THREADS", "4")
os.environ.setdefault("MKL_NUM_THREADS", "4")


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(8 * 1024 * 1024):
            value.update(block)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rlinf-root", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-provenance", type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists(), "Refuse to replace existing model"
    provenance = json.loads(args.source_provenance.read_text())
    assert Path(provenance["remote_source"]).resolve() == args.rlinf_root.resolve()
    manifest = json.loads((args.input / "SOURCE_MANIFEST.json").read_text())
    for record in manifest["files"]:
        path = args.input / record["path"]
        assert path.stat().st_size == record["size"]
        assert sha256(path) == record["sha256"], str(path)
    sys.path.insert(0, str(args.rlinf_root.resolve()))
    import torch
    import jax
    from rlinf.utils.ckpt_convertor.openpi import jax_to_openpi_rlinf as converter
    from rlinf.utils.ckpt_convertor.openpi._core import save_safetensors, write_config_json
    from rlinf.models.embodiment.openpi_rlinf.pi0_model.pi0_config import Pi0Config

    torch.set_num_threads(4)
    jax_devices = [str(device) for device in jax.devices()]
    assert all(device.platform == "cpu" for device in jax.devices())
    source_files = {}
    for name in ("jax_to_openpi_rlinf.py", "_core.py"):
        path = args.rlinf_root / "rlinf/utils/ckpt_convertor/openpi" / name
        source_files[str(path.relative_to(args.rlinf_root))] = sha256(path)
    print("Restoring verified official base on CPU", flush=True)
    params = converter._load_jax_params(args.input)
    weights = {}
    for part in (converter.convert_siglip(params), converter.convert_llm(params, True),
                 converter.convert_projections(params, True)):
        for key, tensor in part.items():
            assert key not in weights
            assert tensor.dtype == torch.float32, (key, tensor.dtype)
            assert bool(torch.isfinite(tensor).all()), key
            weights[key] = tensor.contiguous()
    del params
    config = dict(action_dim=32, action_horizon=50, max_token_len=200,
                  paligemma_variant="gemma_2b", action_expert_variant="gemma_300m",
                  pi05=True, pcd=False, dtype="bfloat16")
    with torch.device("meta"):
        model = Pi0Config(**config).create()
    expected = model.state_dict()
    assert set(weights) == set(expected), dict(
        missing=sorted(set(expected) - set(weights)), unexpected=sorted(set(weights) - set(expected)))
    for key, tensor in weights.items():
        assert tensor.shape == expected[key].shape, (key, tensor.shape, expected[key].shape)
    del model, expected
    partial = args.output.with_name(args.output.name + ".converting")
    partial.mkdir(mode=0o750, parents=False, exist_ok=False)
    print("Saving", len(weights), "validated fp32 tensors", flush=True)
    save_safetensors(weights, partial / "model.safetensors")
    write_config_json(config, partial)
    report = dict(
        result="PASS", created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        input=str(args.input), official_source=manifest["official_source"],
        source_revision_manifest_sha256=manifest["revision_manifest_sha256"],
        converter_files_sha256=source_files, target=str(args.output),
        conversion_driver_sha256=sha256(Path(__file__).resolve()), jax_devices=jax_devices,
        source_provenance_sha256=sha256(args.source_provenance),
        rlinf_commit=provenance["rlinf_commit"], rlinf_archive_sha256=provenance["archive_sha256"],
        tensor_count=len(weights), parameter_count=sum(t.numel() for t in weights.values()),
        storage_dtype="float32", eval_dtype_hint="bfloat16", norm_stats_included=False,
        training_ready=False, gpu_used=False,
        validation="official input hashes; finite fp32 tensors; exact meta-model keys and shapes",
        files={name: dict(size=(partial / name).stat().st_size, sha256=sha256(partial / name))
               for name in ("model.safetensors", "config.json")})
    (partial / "CONVERSION_MANIFEST.json").write_text(json.dumps(report, indent=2) + "\n")
    partial.rename(args.output)
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()

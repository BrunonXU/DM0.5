#!/usr/bin/env python3
"""CPU-only import, isolation, video and synthetic LeRobot runtime smoke.

Writes one new JSON report, removes its own temporary synthetic artifacts, and
fails on any failed runtime check. Does not start Ray, CUDA, training or a robot.
The caller provides an isolated interpreter and the personal FFmpeg LD path.
"""
from __future__ import annotations

import argparse
import datetime
from fractions import Fraction
import hashlib
import importlib
import importlib.metadata as metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import site
import subprocess
import sys
import tempfile
import time
import traceback

# These must precede all third-party imports. No network is needed by this test.
os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["JAX_PLATFORMS"] = "cpu"
os.environ["XLA_PYTHON_CLIENT_PREALLOCATE"] = "false"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["WANDB_MODE"] = "offline"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ.setdefault("OMP_NUM_THREADS", "4")
os.environ.setdefault("MKL_NUM_THREADS", "4")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def under(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def package_checks(args) -> dict:
    prefix = Path(sys.prefix)
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("Run this script using an isolated venv")
    if sys.version_info[:3] != (3, 11, 14):
        raise RuntimeError(f"Expected Python 3.11.14, found {sys.version}")
    if site.ENABLE_USER_SITE:
        raise RuntimeError("User site-packages are enabled")
    global_package_paths = []
    for entry in sys.path:
        if entry and ("site-packages" in Path(entry).parts or "dist-packages" in Path(entry).parts):
            if not under(Path(entry), prefix):
                global_package_paths.append(entry)
    if global_package_paths:
        raise RuntimeError(f"Global/user site-packages leaked into sys.path: {global_package_paths}")

    libdir = args.ffmpeg_lib.resolve()
    configured = {str(Path(p).resolve()) for p in os.environ.get("LD_LIBRARY_PATH", "").split(":") if p}
    if str(libdir) not in configured:
        raise RuntimeError("Personal FFmpeg directory is absent from caller LD_LIBRARY_PATH")
    libraries = {}
    for name in ("libavcodec.so.58", "libavformat.so.58", "libavutil.so.56", "libswscale.so.5"):
        path = libdir / name
        if not path.exists():
            raise RuntimeError(f"Missing FFmpeg4 runtime library: {path}")
        libraries[name] = str(path.resolve())

    expected = {
        "torch": "2.7.1+cu128", "torchvision": "0.22.1+cu128", "torchcodec": "0.3.0",
    }
    if args.lerobot_mode == "v21":
        expected.update({
        "numpy": "1.26.4", "rlinf-openpi": "0.1.1", "rlinf-transformer-openpi": "4.53.2",
        "tokenizers": "0.21.4", "huggingface-hub": "0.35.3", "lerobot": "0.3.3",
        "datasets": "3.6.0", "pyarrow": "20.0.0", "accelerate": "1.7.0",
        "gymnasium": "0.29.1", "av": "14.2.0", "jax": "0.5.3", "jaxlib": "0.5.3",
        "orbax-checkpoint": "0.11.13", "ray": "2.47.0", "hydra-core": "1.3.2",
        "omegaconf": "2.3.0", "torchdata": "0.11.0",
        })
    else:
        expected.update({"lerobot": "0.4.3", "datasets": "4.1.1", "pyarrow": "21.0.0", "av": "15.1.0"})
    versions = {}
    for name, version in expected.items():
        dist = metadata.distribution(name)
        if dist.version != version:
            raise RuntimeError(f"{name}: expected {version}, found {dist.version}")
        if not under(Path(dist.locate_file("")), prefix):
            raise RuntimeError(f"{name} distribution resolves outside the venv")
        versions[name] = dist.version

    providers = []
    for dist in metadata.distributions():
        name = normalize(dist.metadata.get("Name", ""))
        if args.lerobot_mode == "v21" and name == "transformers":
            raise RuntimeError("Stock transformers distribution is installed alongside the fork")
        if any(str(f).replace("\\", "/").startswith("transformers/") for f in dist.files or []):
            providers.append(name)
    if args.lerobot_mode == "v21" and providers != ["rlinf-transformer-openpi"]:
        raise RuntimeError(f"Expected one transformer namespace provider, found {providers}")
    if args.lerobot_mode == "v3":
        from packaging.version import Version
        if Version(metadata.version("numpy")) < Version("2"):
            raise RuntimeError("The v3 data environment must satisfy rerun-sdk's NumPy>=2 constraint")
        for name in ("rlinf-openpi", "rlinf-transformer-openpi"):
            try:
                metadata.distribution(name)
            except metadata.PackageNotFoundError:
                pass
            else:
                raise RuntimeError(f"Do not mix the v3 data environment with {name}")
    pip_check = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True)
    if pip_check.returncode:
        raise RuntimeError(f"pip check failed: {pip_check.stdout}\n{pip_check.stderr}")
    return dict(python=sys.version, executable=sys.executable, prefix=sys.prefix,
                base_prefix=sys.base_prefix, user_site_enabled=site.ENABLE_USER_SITE,
                checked_versions=versions, transformers_providers=providers,
                ffmpeg_libraries=libraries, pip_check=pip_check.stdout.strip())


def import_checks(args) -> dict:
    modules = ["torch", "torchvision", "torchcodec", "numpy", "av", "datasets", "lerobot.datasets.lerobot_dataset"]
    if args.lerobot_mode == "v21":
        modules.extend([
        "jax", "orbax.checkpoint", "transformers",
        "rlinf.config", "rlinf.hybrid_engines.fsdp",
        "rlinf.workers.sft.fsdp_vla_sft_worker",
        "rlinf.models.embodiment.openpi_rlinf", "openpi.training.data_loader",
        ])
    origins = {}
    for name in modules:
        print("Import", name, flush=True)
        module = importlib.import_module(name)
        filename = getattr(module, "__file__", None)
        if not filename:
            raise RuntimeError(f"Cannot identify module origin for {name}")
        allowed = args.rlinf_root if name.startswith("rlinf.") else Path(sys.prefix)
        if not under(Path(filename), allowed):
            raise RuntimeError(f"Unexpected import origin for {name}: {filename}")
        origins[name] = str(Path(filename).resolve())
    import torch
    from lerobot.datasets.lerobot_dataset import CODEBASE_VERSION
    expected_format = "v2.1" if args.lerobot_mode == "v21" else "v3.0"
    if CODEBASE_VERSION != expected_format:
        raise RuntimeError(f"Unexpected LeRobot format: {CODEBASE_VERSION}")
    devices = []
    if args.lerobot_mode == "v21":
        import jax
        import ray
        import transformers
        if transformers.__version__ != "4.53.2":
            raise RuntimeError(f"Unexpected transformers runtime version: {transformers.__version__}")
        if ray.is_initialized():
            raise RuntimeError("Import path unexpectedly initialized Ray")
        devices = [str(device) for device in jax.devices()]
        if any(device.platform != "cpu" for device in jax.devices()):
            raise RuntimeError("JAX initialized a non-CPU device")
    if torch.cuda.is_initialized():
        raise RuntimeError("Import path unexpectedly initialized CUDA")
    torch.set_num_threads(4)
    return dict(origins=origins, jax_devices=devices, lerobot_codebase_version=CODEBASE_VERSION,
                ray_started=False, cuda_initialized=False)


def video_checks(work: Path) -> dict:
    import av
    import numpy as np
    import torch
    from torchcodec.decoders import VideoDecoder

    path = work / "synthetic-video.mp4"
    fps, count, height, width = 12, 12, 48, 64
    originals = []
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("libx264", rate=fps)
        stream.width = width
        stream.height = height
        stream.pix_fmt = "yuv420p"
        stream.options = {"crf": "12", "preset": "fast", "g": "1", "bf": "0"}
        stream.codec_context.thread_count = 1
        for index in range(count):
            pixels = np.empty((height, width, 3), dtype=np.uint8)
            pixels[:] = (30 + index * 10, 50 + index * 7, 70 + index * 5)
            originals.append(pixels)
            frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
            frame.pts = index
            frame.time_base = Fraction(1, fps)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    with av.open(str(path)) as source:
        decoded_pyav = [frame.to_ndarray(format="rgb24") for frame in source.decode(video=0)]
    if len(decoded_pyav) != count:
        raise RuntimeError("PyAV decoded the wrong frame count")
    decoder = VideoDecoder(str(path), device="cpu")
    if len(decoder) != count:
        raise RuntimeError("TorchCodec decoded the wrong frame count")
    indices = [0, count // 2, count - 1]
    frame_batch = decoder.get_frames_at(indices=indices)
    data = frame_batch.data
    if tuple(data.shape) != (len(indices), 3, height, width) or data.device.type != "cpu":
        raise RuntimeError(f"Unexpected TorchCodec frame shape/device: {data.shape}/{data.device}")
    if data.dtype != torch.uint8:
        raise RuntimeError(f"Expected uint8 RGB frames, found {data.dtype}")
    errors = []
    cross_decoder_errors = []
    for decoded, index in zip(data, indices, strict=True):
        actual = decoded.permute(1, 2, 0).numpy().astype(np.int16)
        expected = originals[index].astype(np.int16)
        errors.append(float(np.abs(actual - expected).mean()))
        cross_decoder_errors.append(float(np.abs(actual - decoded_pyav[index].astype(np.int16)).mean()))
    if max(errors) > 8 or max(cross_decoder_errors) > 3:
        raise RuntimeError(f"Unexpected RGB video decode error: {errors}/{cross_decoder_errors}")
    pts = frame_batch.pts_seconds.cpu().numpy()
    np.testing.assert_allclose(pts, np.asarray(indices) / fps, atol=1e-5, rtol=0)
    return dict(encoder="PyAV/libx264", decoder="TorchCodec/CPU", frames=count,
                shape=[count, 3, height, width], checked_indices=indices,
                rgb_mean_absolute_error=errors, pyav_torchcodec_mean_absolute_error=cross_decoder_errors,
                pts_seconds=pts.tolist(), file_bytes=path.stat().st_size, file_sha256=sha256(path))


def dataset_checks(work: Path, args) -> dict:
    import numpy as np
    import pandas as pd
    import torch
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    # This source helper has only stdlib imports. Load it in the data-only
    # environment without importing the full RLinf/OpenPI dependency tree.
    spec = importlib.util.spec_from_file_location("infra_lerobot_compat", args.rlinf_root / "rlinf/data/storage/lerobot/compat.py")
    compat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(compat)

    root = work / f"synthetic-lerobot-{args.lerobot_mode}"
    keys = [f"dimension_{index}" for index in range(16)]
    features = {
        "observation.state": {"dtype": "float32", "shape": (16,), "names": keys},
        "action": {"dtype": "float32", "shape": (16,), "names": keys},
    }
    writer = LeRobotDataset.create(repo_id="local/infra_smoke", fps=30, features=features,
                                   root=root, robot_type="synthetic_infra_only", use_videos=False,
                                   image_writer_threads=0, image_writer_processes=0)
    actions = []
    states = []
    for episode in range(3):
        for frame in range(60):
            action = (np.arange(16, dtype=np.float32) + 1000 * episode + frame).astype(np.float32)
            state = (action * np.float32(0.25)).astype(np.float32)
            actions.append(action.copy())
            states.append(state.copy())
            compat.add_frame_to_dataset(writer, {"observation.state": state, "action": action,
                              "task": f"synthetic episode {episode}"})
        if args.lerobot_mode == "v3":
            writer.save_episode(parallel_encoding=False)
        else:
            writer.save_episode()
    if args.lerobot_mode == "v3":
        writer.finalize()
    reader = LeRobotDataset(repo_id="local/infra_smoke", root=root, video_backend="pyav",
                           delta_timestamps={"action": [index / 30 for index in range(50)]})
    if len(reader) != 180 or reader.num_episodes != 3 or reader.fps != 30:
        raise RuntimeError("Synthetic dataset metadata mismatch")
    starts, ends = compat.episode_boundaries(reader)
    if starts != [0, 60, 120] or ends != [60, 120, 180]:
        raise RuntimeError(f"RLinf episode boundaries mismatch: {starts}/{ends}")
    expected_tasks = pd.DataFrame if args.lerobot_mode == "v3" else dict
    if not isinstance(reader.meta.tasks, expected_tasks):
        raise RuntimeError(f"Unexpected task table type: {type(reader.meta.tasks).__name__}")
    checks = []
    for episode, start in enumerate(starts):
        first = reader[start]
        np.testing.assert_allclose(first["observation.state"].numpy(), states[start], atol=0, rtol=0)
        np.testing.assert_allclose(first["action"].numpy(), np.stack(actions[start:start + 50]), atol=0, rtol=0)
        if tuple(first["action"].shape) != (50, 16) or bool(first["action_is_pad"].any()):
            raise RuntimeError("Complete synthetic H50 window incorrectly padded")
        if first["task"] != f"synthetic episode {episode}":
            raise RuntimeError("Synthetic task text mismatch")
        last = reader[ends[episode] - 1]
        if bool(last["action_is_pad"][0]) or not bool(last["action_is_pad"][1:].all()):
            raise RuntimeError("Episode-tail padding flags mismatch")
        expected_tail = np.repeat(actions[ends[episode] - 1][None], 50, axis=0)
        np.testing.assert_allclose(last["action"].numpy(), expected_tail, atol=0, rtol=0)
        checks.append(dict(episode=episode, first_row=start, full_h50=True, tail_padding_checked=True))
    subset_check = {"checked": False, "reason": "Non-contiguous v2.1 subset semantics are outside this smoke scope"}
    if args.lerobot_mode == "v3":
        subset = LeRobotDataset(repo_id="local/infra_smoke", root=root, episodes=[0, 2], video_backend="pyav",
                               delta_timestamps={"action": [index / 30 for index in range(50)]})
        if len(subset) != 120:
            raise RuntimeError("Wrong synthetic non-contiguous episode subset length")
        selected = subset[60]
        if int(selected["episode_index"]) != 2 or int(selected["index"]) != 120:
            raise RuntimeError("Non-contiguous episode subset index mapping is incorrect")
        np.testing.assert_allclose(selected["action"].numpy(), np.stack(actions[120:170]), atol=0, rtol=0)
        if bool(selected["action_is_pad"].any()):
            raise RuntimeError("Subset full H50 window unexpectedly padded")
        subset_check = {"checked": True, "episodes": [0, 2]}
    if torch.cuda.is_initialized():
        raise RuntimeError("Synthetic dataset check initialized CUDA")
    return dict(codebase_version=reader.meta.info["codebase_version"], episodes=3, frames=180,
                features="synthetic numeric state16/action16 only; video checked separately",
                episode_boundaries=[starts, ends], complete_and_tail_checks=checks,
                non_contiguous_subset=subset_check,
                task_table_type=type(reader.meta.tasks).__name__, task_text_checks=True,
                real_f1_readback=False, openpi_prompt_adapter_validated=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rlinf-root", type=Path, required=True)
    parser.add_argument("--ffmpeg-lib", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lerobot-mode", choices=("v21", "v3"), default="v21",
                        help="v21: OpenPI training environment; v3: separate NumPy2 data-only environment")
    args = parser.parse_args()
    if args.output.exists() or not args.output.parent.is_dir():
        parser.error("Output must be a new file in an existing personal directory")
    if not (args.rlinf_root / "rlinf/config.py").is_file():
        parser.error("rlinf-root must be the deployed source snapshot")
    sys.path.insert(0, str(args.rlinf_root.resolve()))
    report = dict(result="RUNNING", created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  script_sha256=sha256(Path(__file__)), checks={}, gpu_used=False, ray_started=False,
                  training_ready=False, real_f1_data_validated=False,
                  lerobot_mode=args.lerobot_mode,
                  scope="OpenPI training libraries" if args.lerobot_mode == "v21" else "Data-only environment; no OpenPI imports",
                  known_adapter_gap="OpenPI0.1.1 PromptFromLeRobotTask expects dict; LeRobotv3 tasks is DataFrame. F1 adapter pending.")
    started = time.monotonic()
    current = "package_isolation"
    try:
        with tempfile.TemporaryDirectory(prefix="runtime-smoke-", dir=args.output.parent) as directory:
            work = Path(directory)
            # Set these before importing libraries that snapshot environment
            # settings; no persistent global shell/profile is changed.
            os.environ["HF_DATASETS_CACHE"] = str(work / "datasets-cache")
            os.environ["HF_HOME"] = str(work / "huggingface")
            os.environ["HF_LEROBOT_HOME"] = str(work / "lerobot-cache")
            report["checks"][current] = package_checks(args)
            current = "runtime_imports"
            report["checks"][current] = import_checks(args)
            current = "video_codec"
            report["checks"][current] = video_checks(work)
            current = f"synthetic_lerobot_{args.lerobot_mode}"
            report["checks"][current] = dataset_checks(work, args)
        import torch
        ray = sys.modules.get("ray")
        if torch.cuda.is_initialized() or (ray is not None and ray.is_initialized()):
            raise RuntimeError("A smoke check unexpectedly initialized CUDA or Ray")
        report["result"] = "PASS"
        report["synthetic_artifacts_removed"] = True
        report["limitations"] = ["No real F1 dataset, normalization, reader/adapter, training or distributed run",
                                 "Numeric LeRobot and video decoders checked separately, not a real F1 video dataset",
                                 "The v2.1 training environment cannot read current F1 v3 datasets without a future F1 reader/adapter",
                                 "Known OpenPI v3 task-to-prompt adapter gap remains"]
    except Exception as exc:
        report.update(result="FAIL", failed_check=current, error_type=type(exc).__name__,
                      error=str(exc), traceback=traceback.format_exc())
        raise
    finally:
        report["seconds"] = time.monotonic() - started
        report["finished_at_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with args.output.open("x") as output:
            json.dump(report, output, indent=2)
            output.write("\n")
        print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()

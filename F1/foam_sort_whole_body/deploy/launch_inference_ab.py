"""Run the selected 5K/20K services independently of the training container."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess

PROJECTS = Path(__file__).resolve().parents[3]
TASK = PROJECTS / "F1/foam_sort_whole_body"
OUTPUT = TASK / "results/inference_ab_5k_20k_20260918"
IMAGE = "opendm:g2-34d-fa2"
LABEL = "f1-foam-5k-20k-20260918"
SERVICES = {"25k": ("f1-foam-inference-25k", 7896), "5k": ("f1-foam-inference-5k", 7892), "20k": ("f1-foam-inference-20k", 7894),
            "70k": ("f1-foam-inference-70k", 7901)}


def docker(args, *, check=True):
    return subprocess.run(["sg", "docker", "-c", shlex.join(["docker", *args])],
                          check=check, capture_output=True, text=True)


def inspect(name):
    value = docker(["inspect", name], check=False)
    if value.returncode:
        if "no such" not in value.stderr.lower():
            raise RuntimeError(value.stderr)
        return None
    result = json.loads(value.stdout)[0]
    if result["Config"].get("Labels", {}).get("f1.inference.group") != LABEL:
        raise RuntimeError(f"Container name belongs to another task: {name}")
    return result


def sha(data):
    return hashlib.sha256(data).hexdigest()


def start(gpu, keys=("5k", "20k")):
    selected = {key: SERVICES[key] for key in keys}
    existing = {key: inspect(name) for key, (name, _) in SERVICES.items()}
    present = [value for value in existing.values() if value]
    for value in present:
        if value["Config"]["Labels"].get("f1.inference.gpu") != str(gpu):
            raise RuntimeError("Existing services use another GPU; do not silently replace them")
    # Refuse to add a new workload to a card owned by another service/training job.
    rows = subprocess.check_output(["nvidia-smi", "--query-gpu=index,uuid,memory.used", "--format=csv,noheader,nounits"], text=True)
    cards = {int(parts[0]): (parts[1].strip(), int(parts[2]))
             for row in rows.splitlines() if (parts := row.split(","))}
    memory = {index: value[1] for index, value in cards.items()}
    if gpu not in memory:
        raise RuntimeError(f"GPU {gpu} does not exist")
    processes = subprocess.check_output(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid", "--format=csv,noheader,nounits"], text=True)
    owned_pids = {value["State"]["Pid"] for value in present if value["State"]["Running"]}
    selected_pids = {int(row.split(",")[1]) for row in processes.splitlines()
                     if row.split(",")[0].strip() == cards[gpu][0]}
    if selected_pids - owned_pids:
        raise RuntimeError(f"GPU {gpu} has other compute processes: {sorted(selected_pids - owned_pids)}")
    if not any(value["State"]["Running"] for value in present) and memory[gpu] >= 1000:
        raise RuntimeError(f"GPU {gpu} is occupied ({memory[gpu]} MiB); leave other workloads untouched")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    bindings = {}
    sources = {}
    for key in selected:
        config = TASK / f"configs/inference.{key}.json"
        result = subprocess.run(["bash", str(TASK / "scripts/inference_server.sh"), "inspect", "--config", str(config)],
                                check=True, text=True, capture_output=True)
        binding = json.loads(result.stdout)["model_binding"]
        bindings[key] = binding
        for relative, digest in binding["checkpoint_identity"]["binding"]["code"].items():
            data = (PROJECTS / relative).read_bytes()
            if sha(data) != digest:
                raise RuntimeError(f"Training-bound source changed: {relative}")
            sources[relative] = data
        sources[str(config.relative_to(PROJECTS))] = config.read_bytes()
    if any(b["checkpoint_identity"]["binding"] != next(iter(bindings.values()))["checkpoint_identity"]["binding"] for b in bindings.values()):
        raise RuntimeError("Checkpoint training contracts differ")
    for path in [*(TASK / "models/DM0.5/f1_inference" / name
                   for name in ("__init__.py", "__main__.py", "policy.py", "server.py")),
                 TASK / "models/DM0.5/experiment.yaml", TASK / "scripts/inference_server.sh"]:
        sources[str(path.relative_to(PROJECTS))] = path.read_bytes()
    manifest = {relative: sha(data) for relative, data in sorted(sources.items())}
    snapshot_id = sha(json.dumps(manifest, sort_keys=True).encode())[:16]
    # Validate every service before starting either, so an occupied second port
    # does not leave an undocumented partial launch.
    for key, (_, port) in selected.items():
        value = existing[key]
        if value and value["Config"]["Labels"].get("f1.inference.snapshot") != snapshot_id:
            raise RuntimeError(f"{key} source differs; review before replacing the existing service")
        if value is None or not value["State"]["Running"]:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", port))
    snapshot = OUTPUT / "server_sources" / snapshot_id
    for relative, data in sources.items():
        destination = snapshot / relative
        if destination.exists() and destination.read_bytes() != data:
            raise RuntimeError("Existing server source snapshot changed")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            destination.write_bytes(data)
    (snapshot / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    receipt = {"started_at": datetime.now(timezone.utc).isoformat(), "image": IMAGE,
               "physical_gpu": gpu, "shared_gpu": True, "source_snapshot": str(snapshot),
               "model_bindings": bindings, "services": {}, "training_modified": False,
               "prelaunch_gpu_memory_mib": memory[gpu], "status": "starting"}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    receipt_path = OUTPUT / f"launch_receipt_{stamp}.json"
    def save_receipt():
        receipt_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n")
    save_receipt()
    for key, (name, port) in selected.items():
        value = existing[key]
        if value:
            if value["Config"]["Labels"].get("f1.inference.snapshot") != snapshot_id:
                raise RuntimeError(f"{name} source differs; review before replacing the existing service")
            if not value["State"]["Running"]:
                docker(["start", name])
            receipt["services"][key] = {"name": name, "port": port, "reused": True}
            save_receipt()
            continue
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))
        args = ["run", "-d", "--name", name, "--network", "host", "--gpus", f"device={gpu}",
                "--cpus", "8", "--memory", "32g", "--shm-size", "2g", "--read-only",
                "--tmpfs", "/tmp:rw,nosuid,size=4g", "--user", f"{os.getuid()}:{os.getgid()}",
                "--label", f"f1.inference.group={LABEL}", "--label", f"f1.inference.gpu={gpu}",
                "--label", f"f1.inference.snapshot={snapshot_id}",
                "--log-opt", "max-size=50m", "--log-opt", "max-file=5",
                "-v", f"{PROJECTS}:{PROJECTS}:ro", "-v", "/mnt/dm05_nvme/dm05:/mnt/dm05_nvme/dm05:ro"]
        env = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "USER": "user",
               "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "NO_ALBUMENTATIONS_UPDATE": "1",
               "TOKENIZERS_PARALLELISM": "false", "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "1",
               "TORCHINDUCTOR_CACHE_DIR": "/tmp/torchinductor", "TRITON_CACHE_DIR": "/tmp/triton",
               "CUDA_CACHE_PATH": "/tmp/cuda", "NUMBA_CACHE_DIR": "/tmp/numba",
               "XDG_CACHE_HOME": "/tmp/cache", "F1_INFERENCE_PYTHON": "/opt/conda/envs/opendm/bin/python"}
        for key_env, value_env in env.items():
            args += ["-e", f"{key_env}={value_env}"]
        for relative in sources:
            args += ["-v", f"{snapshot / relative}:{PROJECTS / relative}:ro"]
        args += ["--entrypoint", "bash", IMAGE, str(TASK / "scripts/inference_server.sh"),
                 "serve", "--config", str(TASK / f"configs/inference.{key}.json")]
        container_id = docker(args).stdout.strip()
        receipt["services"][key] = {"name": name, "port": port, "container_id": container_id,
                                    "reused": False, "command": shlex.join(["docker", *args])}
        save_receipt()
    receipt["status"] = "started_health_not_yet_checked"
    save_receipt()
    print(json.dumps({"physical_gpu": gpu,
                      "services": {key: {k: v for k, v in value.items() if k != "command"}
                                   for key, value in receipt["services"].items()},
                      "receipt": str(receipt_path)}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "status", "stop"))
    parser.add_argument("--gpu", type=int, default=1)
    parser.add_argument("--checkpoints", nargs="+", choices=tuple(SERVICES), default=["5k", "20k"])
    args = parser.parse_args()
    selected = {key: SERVICES[key] for key in args.checkpoints}
    if args.command == "start":
        start(args.gpu, args.checkpoints)
    else:
        for key, (name, port) in selected.items():
            value = inspect(name)
            if args.command == "stop" and value and value["State"]["Running"]:
                docker(["stop", "--time", "15", name])
                value = inspect(name)
            print(json.dumps({"checkpoint": key, "container": name, "port": port,
                              "state": None if value is None else value["State"]["Status"]}))


if __name__ == "__main__":
    main()

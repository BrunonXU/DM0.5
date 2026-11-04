#!/usr/bin/env python3
"""6000D: prepare or activate a checkpoint of the frozen 15-demo V1 run.

Only packaging, service lifecycle and model binding change. Never sends motion.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import shlex
import subprocess
import time
from urllib.request import urlopen

TASK = Path(__file__).resolve().parents[1]
PROJECTS = TASK.parents[1]
ART = TASK / "artifacts/20261016-v1-formal-training"
RUN = "/u01/user/runs/f1-toys-v1-formal-20261016-r3"
RELEASE = "/home/user/projects/task-releases/f1-toys-20261016-v1-formal-r3"
DATA = "/u01/user/datasets/f1_toys_v1_formal_20261016/dataset/train"
PYTHON = "/u01/user/envs/f1-jimu-pi05-v1/bin/python"
F1_SSH = ["ssh", "-F", str(PROJECTS / "F1-4242/results/pose_task_20260928/ssh_config"), "f1-09-deploy"]


def run(argv, **kwargs):
    return subprocess.run(argv, check=True, text=True, **kwargs)


def docker(*args, **kwargs):
    return run(["sg", "docker", "-c", shlex.join(["docker", *args])], **kwargs)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b""):
            h.update(block)
    return h.hexdigest()


def verify(bundle):
    m = json.loads((bundle / "bundle.json").read_text())
    for name, expected in m["files_sha256"].items():
        if digest(bundle / name) != expected:
            raise ValueError("Bundle checksum mismatch: " + name)
    train = ART / "dataset/train"
    for name in ("contract.json", "norm_stats.json"):
        if digest(bundle / name) != digest(train / name):
            raise ValueError("Bundle differs from this training run: " + name)
    return digest(bundle / "bundle.json")


def prepare(step):
    bundle = ART / f"bundle-{step}"
    if (bundle / "bundle.json").exists():
        verify(bundle)
        return bundle
    checkpoint = f"{RUN}/train/v1_formal_eef_lora/checkpoints/global_step_{step}/actor"
    # rng.pt is written after the full weights, optimizer and data cursor.
    run(["ssh", "h20-train", "test -s " + shlex.quote(checkpoint + "/rng.pt")])
    remote_bundle = f"{RUN}/bundle-{step}"
    export = [PYTHON, f"{RELEASE}/tasks/F1_ComplexyToys/scripts/export_eef.py",
              "--checkpoint", checkpoint, "--dataset", DATA, "--rlinf-root", RELEASE + "/RLinf",
              "--tokenizer", "/u01/user/models/tokenizer-1711547605575873/paligemma_tokenizer.model",
              "--base", "/u01/user/models/pi05-base-e1b61876289b52fa-openpi-rlinf-fp32",
              "--output", remote_bundle, "--lora-manifest", RUN + "/train/lora_manifest.json"]
    command = ("if test ! -f " + shlex.quote(remote_bundle + "/bundle.json") + "; then "
               + "OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4 " + shlex.join(export)
               + " > " + shlex.quote(f"{RUN}/export-{step}.log") + " 2>&1; fi")
    run(["ssh", "h20-train", command])
    bundle.mkdir(exist_ok=True)
    # Seed a separate local file for rsync delta transfer; never mutate old bundles.
    if not (bundle / "model.safetensors").exists():
        previous = sorted(ART.glob("bundle-*/model.safetensors"))
        seed = previous[-1] if previous else TASK / "artifacts/20261016-v1-eef/bundle-1000/model.safetensors"
        run(["cp", "--reflink=auto", str(seed), str(bundle / "model.safetensors")])
    # Publish bundle.json last, so an interrupted download cannot appear ready.
    run(["rsync", "-rlt", "--checksum", "--exclude=bundle.json", "--info=stats2",
         "h20-train:" + remote_bundle + "/", str(bundle) + "/"])
    run(["rsync", "-rlt", "h20-train:" + remote_bundle + "/bundle.json", str(bundle / "bundle.pending.json")])
    pending = json.loads((bundle / "bundle.pending.json").read_text())
    for name, expected in pending["files_sha256"].items():
        if digest(bundle / name) != expected:
            raise ValueError("Downloaded file mismatch: " + name)
    (bundle / "bundle.pending.json").replace(bundle / "bundle.json")
    verify(bundle)
    print(f"READY checkpoint={step} bundle={bundle}", flush=True)
    return bundle


def health(port=8096):
    with urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
        return json.load(response)


def wait_health(identity, port=8096):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            h = health(port)
            if h["ready"] and h["model_version"] == identity and h["denoise_steps"] == 5:
                return h
        except (OSError, ValueError):
            pass
        time.sleep(2)
    raise TimeoutError(f"Model did not become ready on {port}")


def launch(bundle, name, port):
    run(["sg", "docker", "-c", shlex.join(["env", f"F1_TOYS_PORT={port}", "bash",
        str(TASK / "scripts/start_6000d.sh"), str(bundle), name])])


def ensure_policy_tunnel():
    """Restore a missing owned tunnel; verify it before stopping any service."""
    run(["bash", str(TASK / "scripts/start_policy_tunnel.sh")])
    expected = health()
    probe = """import json,time
from urllib.request import urlopen
for attempt in range(10):
 try:
  with urlopen('http://127.0.0.1:8096/health',timeout=3) as response:
   print(json.dumps(json.load(response)))
  break
 except OSError:
  if attempt==9: raise
  time.sleep(.5)
"""
    result = run(F1_SSH + ["python3 -c " + shlex.quote(probe)], capture_output=True)
    if json.loads(result.stdout) != expected:
        raise RuntimeError("F1 tunnel reaches a different model; current service was not changed")


# Hold the robot's existing control lock throughout the service/config switch.
# No ROS imports, robot service starts, or action publishers.
REMOTE_BIND = r'''
import fcntl,json,os,sys
from pathlib import Path
from datetime import datetime,timezone
from urllib.request import urlopen
stage='control_lock'; changed=False; committed=False
try:
 lock=open('/home/user/Bruno/F1/runtime/control.lock','a+')
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 stage='read_config'
 config=Path('/home/user/Bruno/DAgger-runtime/f1-toys-20261016/config-standalone.json')
 original=config.read_bytes(); cfg=json.loads(original)
 print(json.dumps({'locked':True,'old_model_id':cfg['policy']['model_id']}),flush=True)
 stage='request';request=json.loads(sys.stdin.readline())
 stage='tunnel_health'
 with urlopen('http://127.0.0.1:8096/health',timeout=5) as response: actual=json.load(response)
 stage='model_identity'
 assert actual==request['health'], 'F1 tunnel identity mismatch'
 assert actual['protocol']=='f1_eef_sync_v2' and actual['denoise_steps']==5
 assert cfg['policy']['chunk_steps']==20 and cfg['policy']['frequency_hz']==30
 stage='write_config'
 backup=config.parent/'backups'/('checkpoint-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'))
 backup.mkdir(parents=True); (backup/config.name).write_bytes(original)
 cfg['policy']['model_id']=actual['model_id']
 pending=config.with_suffix('.json.next');pending.write_text(json.dumps(cfg,indent=2)+'\n');os.replace(pending,config)
 changed=True
 print(json.dumps({'bound':True,'backup':str(backup),'model_id':actual['model_id'],'robot_commands':0}),flush=True)
 stage='commit';committed=sys.stdin.readline().strip()=='COMMIT'
except Exception as exc:
 print(json.dumps({'error':type(exc).__name__+': '+str(exc),'stage':stage}),flush=True)
 sys.exit(1)
finally:
 if changed and not committed:
  pending.write_bytes(original);os.replace(pending,config)
'''


def line(process):
    if not select.select([process.stdout], [], [], 30)[0]:
        raise TimeoutError("F1 binding response timed out")
    value = process.stdout.readline()
    if not value:
        raise RuntimeError("F1 binding process exited; inspect the SSH/remote error above")
    response = json.loads(value)
    if "error" in response:
        raise RuntimeError(f"F1 binding failed [{response.get('stage', 'unknown')}]: {response['error']}")
    return response


def activate(step, bundle):
    try:
        ensure_policy_tunnel()
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("F1 tunnel preflight failed; current model was not changed: " + (exc.stderr or str(exc))) from exc
    identity = verify(bundle)
    name = f"f1-toys-v1-formal-{step}-5step"
    candidate = name + "-check"
    active_file = ART / "active-checkpoint.json"
    active = json.loads(active_file.read_text()) if active_file.exists() else {"container": "f1-toys-eef-1000-5step"}
    if active.get("step") == step and health()["model_version"] == identity:
        print(f"Already active: {step}"); return
    # Validate on a separate port before touching the current service.
    launch(bundle, candidate, 8097)
    try:
        wait_health(identity, 8097)
        output = "/logs/check-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + ".json"
        docker("exec", candidate, "/opt/conda/envs/opendm/bin/python", "/workspace/tasks/F1_ComplexyToys/scripts/check_checkpoint_service.py",
               "--endpoint", "http://127.0.0.1:8097", "--dataset",
               "/workspace/tasks/F1_ComplexyToys/artifacts/20261016-v1-formal-training/dataset/validation",
               "--output", output)
    finally:
        docker("stop", candidate)
    lock = subprocess.Popen(F1_SSH + ["python3 -u -c " + shlex.quote(REMOTE_BIND)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    switched = False
    try:
        assert line(lock)["locked"]
        docker("stop", active["container"])
        switched = True
        launch(bundle, name, 8096)
        h = wait_health(identity)
        lock.stdin.write(json.dumps({"health": h}) + "\n"); lock.stdin.flush()
        receipt = line(lock)
        assert receipt["bound"]
        receipt.update(step=step, container=name, bundle=str(bundle), health=h,
                       previous=active, time_utc=datetime.now(timezone.utc).isoformat())
        pending = active_file.with_suffix('.json.next')
        pending.write_text(json.dumps(receipt, indent=2)+'\n'); pending.replace(active_file)
        (ART / f"activation-{step}-{time.time_ns()}.json").write_text(json.dumps(receipt,indent=2)+'\n')
        lock.stdin.write("COMMIT\n"); lock.stdin.flush(); lock.stdin.close()
        if lock.wait(timeout=15): raise RuntimeError("F1 binding did not commit")
        print(f"ACTIVE checkpoint={step}; F1 onsite command unchanged. No robot motion started.", flush=True)
    except BaseException:
        if lock.stdin and not lock.stdin.closed: lock.stdin.close()
        if switched:
            subprocess.run(["sg", "docker", "-c", shlex.join(["docker", "stop", name])])
            docker("start", active["container"])
            active_file.write_text(json.dumps(active, indent=2)+'\n')
        raise
    finally:
        if lock.poll() is None:
            lock.wait(timeout=15)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["prepare", "activate", "watch"])
    p.add_argument("step", type=int, nargs="?")
    a = p.parse_args()
    if a.action != "watch" and (a.step is None or a.step not in range(500, 50001, 500)):
        p.error("step must be 500, 1000, ..., 50000")
    ART.mkdir(exist_ok=True)
    def one(step, activate_now=False):
        with (ART / f"bundle-{step}.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            bundle = prepare(step)
        if activate_now:
            with (ART / "service-switch.lock").open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                activate(step, bundle)
    if a.action == "watch":
        for step in range(500, 6001, 500):
            while True:
                try:
                    one(step); break
                except subprocess.CalledProcessError:
                    print(f"checkpoint={step} not ready; retry in 60s", flush=True)
                    time.sleep(60)
        return
    one(a.step, a.action == "activate")


if __name__ == "__main__":
    main()

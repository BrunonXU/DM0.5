#!/usr/bin/env python3
"""Read-only H20 inventory via the existing 6000D h20-train SSH alias.

No sudo, package installs, file scans, GPU workloads, or remote file writes.
The output includes command failures/permissions so unknowns remain explicit.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess


REMOTE_PROBE = r'''
import glob
import importlib.metadata
import json
import os
from pathlib import Path
import resource
import shutil
import socket
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

started = datetime.now(timezone.utc)
result = {
    "started_at_utc": started.isoformat(),
    "started_at_shanghai": started.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
    "hostname": socket.gethostname(), "uid": os.getuid(), "gid": os.getgid(),
    "python_executable": sys.executable, "python_version": sys.version,
    "commands": {}, "files": {},
}

def read_file(name):
    try:
        return Path(name).read_text().strip()
    except (OSError, UnicodeError) as exc:
        return {"error": str(exc)}

def command(key, argv, timeout=20):
    start = time.monotonic()
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           env=dict(os.environ, LC_ALL="C"), timeout=timeout)
        value = {"argv": argv, "returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        value = {"argv": argv, "error": str(exc)}
    value["seconds"] = time.monotonic() - start
    result["commands"][key] = value

commands = [
    ("identity", ["id"]),
    ("kernel", ["uname", "-a"]),
    ("uptime", ["uptime"]),
    ("cpu", ["lscpu", "-J"]),
    ("memory", ["free", "-b"]),
    ("swap", ["swapon", "--show", "--bytes"]),
    ("filesystems_bytes", ["df", "-B1", "-T", "/", "/boot", "/boot/efi", "/home", "/u01", "/tmp", "/dev/shm"]),
    ("filesystems_human", ["df", "-hT"]),
    ("inodes", ["df", "-i", "/", "/home", "/u01"]),
    ("mounts", ["findmnt", "-J", "-t", "ext4,xfs,btrfs,zfs,nfs,nfs4,cifs", "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"]),
    ("block_devices", ["lsblk", "-b", "-J", "-o", "NAME,KNAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS,PKNAME,MODEL,ROTA,RO,TRAN"]),
    ("lvm_physical", ["pvs", "--readonly", "--reportformat", "json", "--units", "b", "--nosuffix", "-o", "pv_name,vg_name,pv_size,pv_free"]),
    ("lvm_groups", ["vgs", "--readonly", "--reportformat", "json", "--units", "b", "--nosuffix", "-o", "vg_name,vg_size,vg_free,pv_count,lv_count"]),
    ("lvm_volumes", ["lvs", "--readonly", "--reportformat", "json", "--units", "b", "--nosuffix", "-o", "vg_name,lv_name,lv_size,segtype,devices"]),
    ("pci_devices", ["lspci", "-nn"]),
    ("gpu_summary", ["nvidia-smi"]),
    ("gpu_list", ["nvidia-smi", "-L"]),
    ("gpu_inventory", ["nvidia-smi", "--query-gpu=index,name,uuid,pci.bus_id,driver_version,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw,power.limit,compute_mode,persistence_mode,mig.mode.current", "--format=csv"]),
    ("gpu_compute_capability", ["nvidia-smi", "--query-gpu=index,compute_cap", "--format=csv"]),
    ("gpu_topology", ["nvidia-smi", "topo", "-m"]),
    ("gpu_links", ["nvidia-smi", "nvlink", "-s"]),
    ("gpu_compute_processes", ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_memory", "--format=csv"]),
    ("gpu_ecc", ["nvidia-smi", "-q", "-d", "ECC,PAGE_RETIREMENT,ROW_REMAPPER"]),
    ("service_states", ["systemctl", "show", "--no-pager", "-p", "Id", "-p", "LoadState", "-p", "ActiveState", "-p", "SubState", "nvidia-persistenced.service", "nvidia-fabricmanager.service", "docker.service", "containerd.service", "slurmd.service", "ssh.service"]),
    ("packages", ["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\n", "nvidia-*", "cuda-*", "libcudnn*", "libnccl*", "docker*", "containerd*", "slurm*", "openssh-server"]),
    ("python_pip", ["python3", "-m", "pip", "--version"]),
    ("compiler", ["gcc", "--version"]),
    ("glibc", ["ldd", "--version"]),
    ("git", ["git", "--version"]),
    ("rsync", ["rsync", "--version"]),
    ("tmux", ["tmux", "-V"]),
    ("interfaces", ["ip", "-j", "address", "show"]),
    ("route_to_6000d", ["ip", "route", "get", "192.0.2.1"]),
    ("ssh_listeners", ["ss", "-ltn", "sport = :22 or sport = :6622"]),
    ("linger", ["loginctl", "show-user", "user", "-p", "Linger"]),
]
for key, argv in commands:
    command(key, argv)

for name in ["/etc/os-release", "/etc/timezone", "/proc/meminfo", "/proc/mdstat",
             "/proc/self/cgroup", "/proc/driver/nvidia/version", "/proc/net/bonding/bond0",
             "/sys/class/dmi/id/sys_vendor", "/sys/class/dmi/id/product_name",
             "/sys/class/dmi/id/product_version"]:
    result["files"][name] = read_file(name)

for name in glob.glob('/sys/devices/system/node/node*/cpulist') + glob.glob('/sys/devices/system/node/node*/meminfo'):
    result["files"][name] = read_file(name)

result["path_tools"] = {name: shutil.which(name) for name in
    ["python", "python3", "pip", "pip3", "conda", "mamba", "uv", "nvcc", "docker", "podman",
     "apptainer", "singularity", "nvidia-container-cli", "srun", "sbatch", "squeue", "numactl",
     "ibstat", "ibv_devinfo", "rdma", "git", "cmake", "gcc", "g++", "tmux", "rsync", "node", "npm", "codex"]}

# Metadata only: do not import torch/TensorFlow or create CUDA contexts.
result["python_packages"] = {}
for package in ["pip", "torch", "torchvision", "torchaudio", "transformers", "accelerate", "deepspeed",
                "numpy", "jax", "jaxlib", "tensorflow", "nvidia-cuda-runtime-cu12", "nvidia-nccl-cu12"]:
    try:
        result["python_packages"][package] = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        result["python_packages"][package] = None

result["limits"] = {}
for name in ["RLIMIT_NOFILE", "RLIMIT_NPROC", "RLIMIT_MEMLOCK", "RLIMIT_AS", "RLIMIT_STACK", "RLIMIT_CORE"]:
    result["limits"][name] = resource.getrlimit(getattr(resource, name))
result["cpu_affinity"] = sorted(os.sched_getaffinity(0))

result["paths"] = {}
for name in ["/", "/home", "/u01", "/home/user", "/home/user/projects",
             "/u01/user", "/u01/user/datasets", "/u01/user/runs",
             "/u01/user/cache", "/u01/user/staging", "/dev/shm"]:
    p = Path(name)
    try:
        s = p.stat()
        v = os.statvfs(p)
        result["paths"][name] = {"mode": stat.filemode(s.st_mode), "uid": s.st_uid, "gid": s.st_gid,
            "writable": os.access(p, os.W_OK), "mountpoint": os.path.ismount(p),
            "filesystem_size_bytes": v.f_blocks * v.f_frsize,
            "filesystem_free_bytes": v.f_bfree * v.f_frsize,
            "filesystem_available_bytes": v.f_bavail * v.f_frsize}
    except OSError as exc:
        result["paths"][name] = {"error": str(exc)}

# Shallow discovery only, no recursive traversal of other users' data or environments.
result["directory_entries"] = {}
for name in ["/home", "/opt", "/usr/local", "/u01", "/home/user",
             "/home/user/projects", "/u01/user", "/sys/class/infiniband"]:
    try:
        result["directory_entries"][name] = sorted(os.listdir(name))
    except OSError as exc:
        result["directory_entries"][name] = {"error": str(exc)}
result["environment_locations"] = {}
for name in ["/opt/conda", "/opt/miniconda3", "/opt/anaconda3", "/usr/local/cuda",
             "/home/user/miniconda3", "/home/user/anaconda3",
             "/home/user/.venv", "/home/user/.conda"]:
    p = Path(name)
    result["environment_locations"][name] = {"exists": p.exists(), "symlink": p.is_symlink(),
                                            "resolved": str(p.resolve()) if p.exists() else None}
for binary in glob.glob('/usr/local/cuda*/bin/nvcc'):
    command('nvcc_' + binary, [binary, '--version'])

result["proxy_settings"] = {}
for name in ["http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"]:
    value = os.environ.get(name)
    if not value:
        continue
    try:
        p = urlsplit(value if '://' in value else '//' + value)
        result["proxy_settings"][name] = {"scheme": p.scheme, "host": p.hostname, "port": p.port,
                                          "has_credentials": p.username is not None}
    except ValueError:
        result["proxy_settings"][name] = {"set": True, "parse_error": True}

# Inspect unified cgroup limits up the ancestry without changing anything.
cgroup = result['files']['/proc/self/cgroup']
if isinstance(cgroup, str):
    for line in cgroup.splitlines():
        hierarchy, controllers, relative = line.split(':', 2)
        if hierarchy == '0' and not controllers:
            root = Path('/sys/fs/cgroup')
            current = root / relative.lstrip('/')
            if root not in current.parents and current != root:
                continue
            while True:
                for leaf in ['cpu.max', 'cpuset.cpus.effective', 'cpuset.mems.effective', 'memory.max', 'pids.max']:
                    p = current / leaf
                    if p.exists():
                        result['files'][str(p)] = read_file(p)
                if current == root:
                    break
                current = current.parent

result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
print(json.dumps(result, ensure_ascii=False, indent=2))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output; existing inventory snapshots are immutable")
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
           "-o", "HostKeyAlgorithms=ssh-ed25519", "h20-train", "python3 -B -"]
    collected = subprocess.run(ssh, input=REMOTE_PROBE, capture_output=True, text=True, timeout=300)
    if collected.returncode:
        raise SystemExit(f"Inventory SSH failed: {collected.stderr}")
    result = json.loads(collected.stdout)
    result["collection"] = {"source": "6000D", "ssh_alias": "h20-train", "host": "192.0.2.27",
                            "port": 6622, "user": "user", "method": "read-only, no sudo",
                            "received_at_utc": datetime.now(timezone.utc).isoformat()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    failed = [key for key, value in result["commands"].items() if value.get("returncode", 1) != 0]
    print(f"Saved {args.output}; command checks: {len(result['commands'])}; unavailable/failed: {failed}")


if __name__ == "__main__":
    main()

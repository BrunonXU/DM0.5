"""Verify the Full 250 reverse tunnel and exact model; no robot outputs."""
import json
from pathlib import Path
from urllib.request import urlopen

ROOT = Path("/home/user/Bruno/DAgger-runtime/f1-toys-20261016/full-rtc/250")


def main():
    profile = json.loads((ROOT / "profile.json").read_text())
    with urlopen("http://127.0.0.1:8151/health", timeout=5) as response:
        health = json.load(response)
    for key in ("ready", "model_id", "model_version", "protocol",
                "prediction_horizon", "denoise_steps", "rtc", "input_preprocessing"):
        if health.get(key) != profile[key]:
            raise RuntimeError(f"Full 250 identity mismatch: {key}")
    print("Full 250 ready: F1 8151 -> 6000D 8150; " + health["model_id"])


if __name__ == "__main__":
    main()

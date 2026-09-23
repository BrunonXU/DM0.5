"""Extract original compressed frames by existing causal camera source indices."""
from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
from PIL import Image

from .reader import CAMERAS, require, sha256

TOPICS = {"head": "/camera/head/color/image_raw/compressed",
          "left": "/camera/left_wrist/color/image_raw/compressed",
          "right": "/camera/right_wrist/color/image_raw/compressed"}


def extract_images(episode, raw_root, output, starts=None):
    """Write a new immutable cache. No low-rate review thumbnails or video seek.

    Metadata binds index, original MCAP log stamp, payload SHA and source pack.
    ``starts`` can restrict engineering smoke checks; missing frames fail loudly
    when a full dataset later attempts to use that partial cache.
    """
    from mcap.reader import make_reader
    from mcap_ros2.decoder import DecoderFactory

    output = Path(output)
    require(not output.exists(), "image cache output already exists; choose a new path")
    source = Path(raw_root) / episode.meta["source_relative_path"]
    source_stat = source.stat()
    require(source_stat.st_size == episode.meta["source_size_bytes"], "raw MCAP size changed")
    require(source_stat.st_mtime_ns == episode.meta["source_mtime_ns"], "raw MCAP fingerprint changed")
    if starts is None:
        starts = episode.starts
    require(all(int(t) in episode.starts for t in starts), "requested image start is invalid")
    indices = episode.data["camera_source_indices"][starts]
    required = {name: set(indices[:, i].tolist()) for i, name in enumerate(CAMERAS)}
    output.mkdir(parents=True)
    entries, counts = {name: {} for name in CAMERAS}, {name: 0 for name in CAMERAS}
    reverse = {topic: name for name, topic in TOPICS.items()}
    decoder = DecoderFactory()
    with source.open("rb") as stream:
        reader = make_reader(stream, validate_crcs=True)
        for schema, channel, message in reader.iter_messages(topics=list(TOPICS.values()), log_time_order=True):
            name = reverse[channel.topic]
            index = counts[name]
            counts[name] += 1
            if index not in required[name]:
                continue
            require(schema is not None and schema.name == "sensor_msgs/msg/CompressedImage", "unexpected image schema")
            decode = decoder.decoder_for(channel.message_encoding, schema)
            require(decode is not None, "no MCAP image decoder")
            decoded = decode(message.data)
            payload = bytes(decoded.data)
            with Image.open(io.BytesIO(payload)) as image:
                image.load()
                size = list(image.size)
            filename = f"{name}/{index:08d}.image"
            path = output / filename
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(payload)
            entries[name][str(index)] = {"path": filename, "sha256": sha256(path), "size": size,
                                         "log_time_ns": int(message.log_time),
                                         "header_time_ns": int(decoded.header.stamp.sec) * 10**9 + int(decoded.header.stamp.nanosec)}
    for name in CAMERAS:
        require(set(map(int, entries[name])) == required[name], f"missing raw {name} image indices")
    # Check per-observation causal selection, using integer nanoseconds, not float timestamps.
    for t in starts:
        for i, name in enumerate(CAMERAS):
            entry = entries[name][str(episode.data["camera_source_indices"][t, i])]
            age = (int(episode.data["time_ns"][t]) - entry["log_time_ns"]) / 1e9
            require(age >= 0, "future camera frame selected")
            require(abs(age - float(episode.data["camera_log_age_s"][t, i])) < 1e-6, "camera index/log age does not match aligned source")
    manifest = {"schema": "f1_original_compressed_images_v1", "episode": episode.episode,
                "source_npz_sha256": episode.source_sha256, "raw_path": str(source),
                "raw_size_bytes": source_stat.st_size, "raw_mtime_ns": source_stat.st_mtime_ns,
                "checked_starts": list(map(int, starts)), "full_valid_starts": len(starts) == len(episode.starts),
                "training_approved": False, "images": entries}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


class ImageLoader:
    def __init__(self, cache_root, *, training=False):
        self.root = Path(cache_root)
        self.training = training
        self.manifests = {}
        self.transforms = None

    def _build_transforms(self):
        if self.transforms is not None:
            return
        # Exact shared preprocessing/augmentation implementation; no model edits.
        from opendm.data.augmentations import TrainingTransformPipeline, NoAugmentationPipeline
        import albumentations as A
        plain = NoAugmentationPipeline()
        if self.training:
            head = TrainingTransformPipeline(p=.5)
            wrist = A.Compose(plain.steps + [A.ColorJitter(brightness=.3, contrast=.4, saturation=.3, hue=0., p=.5)])
            self.transforms = (head, wrist, wrist)
        else:
            self.transforms = (plain, plain, plain)

    def raw_images(self, episode, indices):
        directory = self.root / episode.episode.replace("/", "_")
        if episode.episode not in self.manifests:
            manifest = json.loads((directory / "manifest.json").read_text())
            require(manifest["source_npz_sha256"] == episode.source_sha256, "image cache/source SHA mismatch")
            require(manifest["episode"] == episode.episode, "image episode mismatch")
            self.manifests[episode.episode] = manifest
        manifest = self.manifests[episode.episode]
        result = []
        for name, index in zip(CAMERAS, indices):
            entry = manifest["images"][name].get(str(index))
            require(entry is not None, f"missing {name} image {index}; partial smoke cache is not a full dataset")
            path = directory / entry["path"]
            require(sha256(path) == entry["sha256"], "changed cached image")
            with Image.open(path) as image:
                result.append(image.convert("RGB"))
        return result

    def __call__(self, episode, indices):
        self._build_transforms()
        return {f"images_{i+1}": Image.fromarray(transform(image=np.asarray(image))["image"]).convert("RGB")
                for i, (image, transform) in enumerate(zip(self.raw_images(episode, indices), self.transforms))}

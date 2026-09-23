"""Pinned data releases and reproducible source-balanced training draws."""
from __future__ import annotations

import json
from pathlib import Path
import numpy as np
from f1_data.reader import CONTRACT, SUPPORTED_CONTRACTS, Episode, F1Dataset, Normalizer, require, sha256
from f1_data.images import ImageLoader


def file_record(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256(path)}


def verify_file(record, root):
    path = (root / record["path"]).resolve()
    require(sha256(path) == record["sha256"], f"release file changed: {path}")
    return path


def describe_release(episodes, statistics, image_root):
    """Describe existing artifacts, never approve data or refit statistics."""
    entries = []
    for ep in episodes:
        images = Path(image_root) / ep.episode.replace("/", "_") / "manifest.json"
        entries.append({"episode": ep.episode, "split": ep.split,
                        "source": file_record(ep.source), "source_metadata": file_record(ep.source.with_suffix('.json')),
                        "poses": file_record(ep.pose_cache), "pose_metadata": file_record(ep.pose_cache.with_suffix('.json')),
                        "images": file_record(images), "valid_starts": ep.starts.tolist()})
    require(len({ep.contract for ep in episodes}) == 1, 'mixed reader contracts')
    return {"schema": "f1_dm05_release_v1", "contract": episodes[0].contract,
            "training_approved": False, "statistics": file_record(statistics),
            "image_root": str(Path(image_root).resolve()), "episodes": entries,
            "evaluation": None}


class Release:
    def __init__(self, path, *, engineering=False):
        self.path = Path(path).resolve()
        self.document = json.loads(self.path.read_text())
        doc, root = self.document, self.path.parent
        require(doc["schema"] == "f1_dm05_release_v1" and doc["contract"] in SUPPORTED_CONTRACTS, "release contract mismatch")
        require(engineering or doc["training_approved"] is True, "formal release is not training approved")
        self.stats_path = verify_file(doc["statistics"], root)
        self.normalizer = Normalizer(json.loads(self.stats_path.read_text()))
        require(engineering or self.normalizer.stats["training_approved"] is True, "engineering statistics cannot train")
        self.image_root = (root / doc["image_root"]).resolve()
        self.episodes = []
        for entry in doc["episodes"]:
            paths = {key: verify_file(entry[key], root) for key in
                     ("source", "source_metadata", "poses", "pose_metadata", "images")}
            require(paths['source_metadata'] == paths['source'].with_suffix('.json') and
                    paths['pose_metadata'] == paths['poses'].with_suffix('.json'), "sidecar path mismatch")
            ep = Episode(paths["source"], paths["poses"], allow_candidates=engineering)
            require(ep.episode == entry["episode"] and ep.split == entry["split"], "release identity/split mismatch")
            require(ep.contract == doc['contract'] == self.normalizer.stats['schema'], 'release/reader/statistics contract mismatch')
            require(ep.starts.tolist() == entry["valid_starts"], "release valid starts changed")
            require(len(ep.starts) > 0, "release contains an empty recording")
            require(paths['images'] == self.image_root / ep.episode.replace('/', '_') / 'manifest.json', "image root mismatch")
            image_meta = json.loads(paths['images'].read_text())
            require(image_meta['source_npz_sha256'] == ep.source_sha256, "image/source mismatch")
            for camera, column in (("head", 0), ("left", 1), ("right", 2)):
                needed = ep.data['camera_source_indices'][ep.starts, column]
                require(all(str(int(i)) in image_meta['images'][camera] for i in needed), "incomplete image cache")
            self.episodes.append(ep)
        require(len({e.episode for e in self.episodes}) == len(self.episodes), "duplicate recording across release/splits")
        require(len({e.source_sha256 for e in self.episodes}) == len(self.episodes), "duplicate source content")
        train = self.for_split("train")
        require(train and self.for_split("validation"), "train and validation are required")
        expected = sorted((e.episode, e.source_sha256, sha256(e.pose_cache), len(e.starts)) for e in train)
        actual = sorted((e['episode'], e['sha256'], e['pose_sha256'], e['valid_starts'])
                        for e in self.normalizer.stats['source_packs'])
        require(actual == expected, "statistics must match exactly the release training sources")
        # Validate the common FK/mapping identity across both splits.
        for split in ("train", "validation"):
            F1Dataset(self.for_split(split), self.normalizer)

    def for_split(self, split):
        return [ep for ep in self.episodes if ep.split == split]

    def dataset(self, split, processor, *, seed=42):
        from f1_data.state_tokens import make_tokenizer
        images = SeededImageLoader(self.image_root, training=split == 'train')
        tokenizer = make_tokenizer(processor, image_keys=['images_1', 'images_2', 'images_3'], max_length=1024)
        return DrawDataset(self.for_split(split), self.normalizer, images, tokenizer, seed=seed)


class SeededImageLoader(ImageLoader):
    """Only seeds existing per-view transforms; probabilities/parameters unchanged."""
    draw_seed = None

    def __call__(self, episode, indices):
        from PIL import Image
        self._build_transforms()
        result = {}
        for camera, (image, transform) in enumerate(zip(self.raw_images(episode, indices), self.transforms)):
            if self.training:
                require(self.draw_seed is not None, "training augmentation requires a sampler draw identity")
                seed = int(np.random.SeedSequence([self.draw_seed, camera]).generate_state(1)[0])
                compose = getattr(transform, 'compose', transform)
                compose.set_random_seed(seed)
            result[f'images_{camera+1}'] = Image.fromarray(transform(image=np.asarray(image))['image']).convert('RGB')
        return result


class DrawDataset(F1Dataset):
    def __init__(self, *args, seed=42, **kwargs):
        super().__init__(*args, **kwargs)
        self.seed = seed

    def __getitem__(self, key):
        if isinstance(key, tuple):
            index, epoch, draw = key
            self.image_loader.draw_seed = int(np.random.SeedSequence([self.seed, epoch, draw]).generate_state(1)[0])
        else:
            require(not self.image_loader.training, "training samples require source-balanced draw keys")
            index = key
        return super().__getitem__(index)


class TrainingDrawSampler:
    """Global draw stream; Accelerate alone shards batches (no second rank split).

    Each draw carries its epoch/ordinal so worker prefetch and checkpoint skip
    cannot alter the image augmentation applied to that draw.
    """
    def __init__(self, dataset, seed=42):
        self.dataset, self.seed, self.epoch = dataset, seed, 0

    def __len__(self):
        return len(self.dataset)

    def set_epoch(self, epoch):
        self.epoch = int(epoch)

    def __iter__(self):
        stream = self.dataset.balanced_indices(len(self), seed=self.seed, epoch=self.epoch)
        for draw, index in enumerate(stream):
            yield (index, self.epoch, draw)

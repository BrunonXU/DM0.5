"""Deterministic global source quotas, then uniform episode and anchor draws."""
from collections import defaultdict
import math

import numpy as np
from torch.utils.data import Sampler


class SourceEpisodeSampler(Sampler):
    """Every global update has an exact quota, independently of rank count.

    Epochs contain an integer number of updates. The sequence is a pure
    function of seed/epoch; StatefulDataLoader persists the consumed cursor.
    Sampler RNG never consumes the image augmentation NumPy stream.
    """

    def __init__(self, rows, *, counts, global_batch, num_replicas, rank, seed,
                 focus_indices=(), focus_multiplier=1.0):
        if (not 0 <= rank < num_replicas or global_batch % num_replicas
                or sum(counts.values()) != global_batch
                or any(type(n) is not int or n <= 0 for n in counts.values())):
            raise ValueError('Invalid global source quotas or rank geometry')
        pools = defaultdict(lambda: defaultdict(list))
        for i, row in enumerate(rows):
            pools[row['dataset_role']][row['episode_id']].append(i)
        if set(pools) != set(counts):
            raise ValueError('Source quotas must cover exactly all training sources')
        self.pools = {role: [np.asarray(pools[role][ep], np.int64)
                            for ep in sorted(pools[role])] for role in sorted(pools)}
        self.counts = dict(counts)
        self.global_batch, self.num_replicas, self.rank = global_batch, num_replicas, rank
        self.seed, self.epoch = int(seed), 0
        self.updates_per_epoch = max(1, math.ceil(len(rows) / global_batch))
        focus = list(focus_indices)
        if (len(set(focus)) != len(focus)
                or any(type(i) is not int or not 0 <= i < len(rows) for i in focus)
                or not math.isfinite(focus_multiplier) or focus_multiplier < 1):
            raise ValueError('Invalid stage focus indices or multiplier')
        self.focus_weights = None
        if focus_multiplier != 1:
            if not focus:
                raise ValueError('Stage focus requires a nonempty audited subset')
            weights = np.ones(len(rows), dtype=np.float64)
            weights[focus] = focus_multiplier
            self.focus_weights = {
                role: [weights[pool] / weights[pool].sum() for pool in episodes]
                for role, episodes in self.pools.items()
            }

    def set_epoch(self, epoch):
        self.epoch = int(epoch)

    def __len__(self):
        return self.updates_per_epoch * self.global_batch // self.num_replicas

    def __iter__(self):
        rng = np.random.default_rng(np.random.SeedSequence([self.seed, self.epoch]))
        # Consume the old RNG exactly as before: both arms retain identical
        # source/episode draws and shuffle order. Only anchor selection changes.
        focus_rng = np.random.default_rng(np.random.SeedSequence([self.seed, self.epoch, 20261022]))
        for _ in range(self.updates_per_epoch):
            group = []
            for role, episodes in self.pools.items():
                for _ in range(self.counts[role]):
                    episode_index = int(rng.integers(len(episodes)))
                    pool = episodes[episode_index]
                    selected = int(pool[int(rng.integers(len(pool)))])
                    if self.focus_weights is not None:
                        selected = int(focus_rng.choice(pool, p=self.focus_weights[role][episode_index]))
                    group.append(selected)
            rng.shuffle(group)
            yield from group[self.rank::self.num_replicas]

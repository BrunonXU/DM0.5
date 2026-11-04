"""Fixed-noise held-out flow loss; preserve all training RNG streams."""
import random

import numpy as np
import torch


def run_validation(worker):
    from rlinf.utils.utils import get_rng_state, set_rng_state

    if worker.eval_data_loader is None:
        raise ValueError("Validation loader is missing")
    previous = get_rng_state()
    training = worker.model.training
    try:
        np.random.seed(int(worker.cfg.actor.seed) + 100000 + worker._rank)
        random.seed(int(worker.cfg.actor.seed) + 100000 + worker._rank)
        torch.manual_seed(int(worker.cfg.actor.seed) + 100000 + worker._rank)
        torch.cuda.manual_seed_all(int(worker.cfg.actor.seed) + 100000 + worker._rank)
        worker.model.eval()
        original = worker.eval_data_loader
        dataset = getattr(getattr(original, 'dataset', None), 'dataset', None)
        loaders = {'all': original}
        if getattr(dataset, 'manifest', {}).get('sampling'):
            from collections import defaultdict
            from torch.utils.data import DataLoader, Subset
            from .dataset import ExactValidationSampler
            groups = defaultdict(list)
            for i in original.dataset.indices:
                row = dataset.rows[i]
                groups[row['dataset_role']+'/'+row['supervision_phase']].append(i)
            loaders = {}
            for name, indices in sorted(groups.items()):
                selected = Subset(dataset, indices)
                sampler = ExactValidationSampler(selected, worker._world_size, worker._rank)
                loaders[name] = DataLoader(selected, batch_size=original.batch_size,
                    sampler=sampler, num_workers=0, collate_fn=original.collate_fn)
        metrics, aggregate, sources = {}, [0., 0], {}
        with torch.no_grad():
            for name, loader in loaders.items():
                totals = torch.zeros(2, dtype=torch.float64, device=worker.device)
                rounds = torch.tensor(len(loader), dtype=torch.int64, device=worker.device)
                torch.distributed.all_reduce(rounds, op=torch.distributed.ReduceOp.MAX)
                batches = iter(loader)
                for _ in range(int(rounds)):
                    batch = next(batches, None)
                    scored = batch is not None
                    if not scored:
                        # Synchronization-only FSDP forward, never a scored sample.
                        batch = loader.collate_fn([loader.dataset[0]])
                    loss, _ = worker.get_train_model_output(batch)
                    size = batch[1].shape[0]
                    if not torch.isfinite(loss):
                        raise ValueError("Nonfinite validation loss")
                    if scored:
                        totals[0] += loss.detach().double() * size
                        totals[1] += size
                torch.distributed.all_reduce(totals, op=torch.distributed.ReduceOp.SUM)
                total, count = float(totals[0]), int(totals[1])
                if count <= 0:
                    raise ValueError("Empty validation group")
                aggregate[0] += total; aggregate[1] += count
                if name != 'all':
                    metrics['loss/'+name], metrics['samples/'+name] = total/count, count
                    role = name.split('/')[0]
                    sums = sources.setdefault(role, [0., 0])
                    sums[0] += total; sums[1] += count
        for role, (total, count) in sources.items():
            metrics['loss/'+role], metrics['samples/'+role] = total/count, count
        return dict(metrics, loss=aggregate[0]/aggregate[1], samples=aggregate[1])
    finally:
        worker.model.train(training)
        set_rng_state(previous)

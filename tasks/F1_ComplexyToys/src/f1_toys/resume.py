"""Recover the epoch omitted by the fixed-length legacy F1 SFT checkpoint."""


def legacy_progress(step, accumulation, batches_per_epoch, loader_state):
    consumed = step * accumulation
    epoch = (consumed - 1) // batches_per_epoch if consumed else 0
    offset = consumed - epoch * batches_per_epoch
    if int(loader_state['_num_yielded']) != offset:
        raise ValueError('Legacy checkpoint cursor disagrees with training step/batch geometry')
    return epoch, offset


def repartition_v1_eight_to_four(all_states, progress, loader, *, rank, step,
                                global_batch, accumulation):
    """Migrate the frozen V1 sampler cursor; never replay image decoding.

    Only the verified 11791-window, micro2, global16, zero-worker layout is
    supported. Global groups of 16 retain their sample IDs; rank RNG streams
    and floating-point reduction order cannot remain bitwise equivalent.
    """
    import copy
    from torch.utils.data import DistributedSampler

    sampler = loader.sampler
    if not (
        len(all_states) == 8 and isinstance(sampler, DistributedSampler)
        and sampler.num_replicas == 4 and sampler.rank == rank
        and sampler.seed == 20261016 and sampler.shuffle and not sampler.drop_last
        and len(loader.dataset) == 11791 and len(loader) == 1474
        and loader.batch_size == 2 and loader.num_workers == 0 and loader.drop_last
        and global_batch == 16 and accumulation == 2
    ):
        raise ValueError('Unsupported V1 eight-to-four batch/sampler geometry')
    epoch, offset = int(progress['epoch']), int(progress['offset'])
    expected_epoch, expected_offset = legacy_progress(
        step, 1, 737, {'_num_yielded': offset}
    )
    if (epoch, offset) != (expected_epoch, expected_offset):
        raise ValueError('V1 epoch disagrees with checkpoint step')
    template = {
        '_index_sampler_state': None,
        '_sampler_iter_state': {'samples_yielded': offset * 2},
        '_sampler_iter_yielded': offset, '_num_yielded': offset,
        '_IterableDataset_len_called': None, '_shared_seed': None,
        'fetcher_state': None, 'dataset_state': None, '_iterator_finished': False,
    }
    if any(state != template for state in all_states):
        raise ValueError('Unsupported or inconsistent saved V1 loader state')
    state = copy.deepcopy(template)
    state['_sampler_iter_state']['samples_yielded'] *= 2
    state['_sampler_iter_yielded'] *= 2
    state['_num_yielded'] *= 2
    return state, epoch, offset * 2

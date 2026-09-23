"""F1 missing state serialization; no fabricated zero-velocity observation."""
import numpy as np


def state_tokens(state, mask, n_bins=256):
    state = np.asarray(state)
    mask = np.asarray(mask)
    if state.shape != (19,) or mask.shape != (19,) or mask.dtype != bool:
        raise ValueError('state19 and bool state_mask19 required')
    if not mask[:16].all() or not np.isfinite(state[mask]).all():
        raise ValueError('Only chassis state may be missing')
    bins = np.floor((np.clip(np.where(mask, state, 0), -1, 1)+1)*.5*(n_bins-1)).astype(int)
    return [int(v) if known else 'unknown' for v, known in zip(bins, mask)]


def make_tokenizer(processor, *, base_class=None, **kwargs):
    if base_class is None:
        from opendm.data.transforms import ChatTokenization
        base_class = ChatTokenization

    class F1StateTokenization(base_class):
        def __call__(self, data):
            self._state_mask = data.get('state_mask', np.ones(19, bool))
            try:
                return super().__call__(data)
            finally:
                self._state_mask = None

        def action_to_bin_tokens(self, action, n_bins=256):
            mask = self._state_mask
            if mask is None:
                raise ValueError('state tokenization called outside sample context')
            return state_tokens(action, mask, n_bins)

    return F1StateTokenization(processor, **kwargs)

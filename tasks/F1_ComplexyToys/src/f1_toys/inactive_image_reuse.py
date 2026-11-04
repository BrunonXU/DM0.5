"""Exact, instance-local inactive vision reuse prepared outside CUDA capture."""
from contextlib import contextmanager
import torch


def _clone(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if torch.is_tensor(value):
        return value.detach().clone()
    if isinstance(value, tuple):
        return tuple(_clone(v) for v in value)
    if isinstance(value, list):
        return [_clone(v) for v in value]
    if isinstance(value, dict):
        return {k: _clone(v) for k, v in value.items()}
    raise TypeError('Unsupported vision output for exact reuse')


class InactiveImageReuse:
    def __init__(self, model):
        self.model = model
        self.entries = {}
        self.hits = 0
        self.resets = 0
        self.parameter_versions = None

    def prepare(self, observation):
        if self.model.training or torch.is_grad_enabled():
            raise RuntimeError('Inactive vision reuse requires frozen no-grad inference')
        versions = tuple((id(p), p._version) for p in self.model.img.parameters())
        if self.parameter_versions is not None and versions != self.parameter_versions:
            raise RuntimeError('Vision weights changed; restart the inference instance')
        self.parameter_versions = versions
        views = [(key, image, bool(observation.image_masks[key].any()))
                 for key, image in observation.images.items()]
        active_ids = {id(image) for _, image, active in views if active}
        eligible = {key: image for key, image, active in views
                    if not active and id(image) not in active_ids}
        signature = (torch.is_autocast_enabled('cuda'), torch.get_autocast_dtype('cuda'))
        changed = eligible.keys() != self.entries.keys()
        if not changed:
            changed = any(entry['signature'] != signature or entry['image'].shape != image.shape
                          or entry['image'].dtype != image.dtype or entry['image'].device != image.device
                          or not torch.equal(entry['image'], image)
                          for key, image in eligible.items() for entry in [self.entries[key]])
        if changed:
            # Called within the owner's prefix-only autocast scope, before capture.
            entries = {key: dict(signature=signature, image=image.detach().clone(),
                                output=_clone(self.model.img(image))) for key, image in eligible.items()}
            self.entries = entries
            self.resets += 1
        elif eligible:
            self.hits += 1
        return changed

    @contextmanager
    def apply(self, observation):
        ids = {id(observation.images[key]): entry['output'] for key, entry in self.entries.items()}
        forward = self.model.img.forward
        had_override = 'forward' in vars(self.model.img)
        def reuse(image, *args, **kwargs):
            if id(image) not in ids:
                return forward(image, *args, **kwargs)
            if args or kwargs:
                raise RuntimeError('Vision call arguments changed')
            return ids[id(image)]
        self.model.img.forward = reuse
        try:
            yield
        finally:
            if had_override:
                self.model.img.forward = forward
            else:
                del self.model.img.forward

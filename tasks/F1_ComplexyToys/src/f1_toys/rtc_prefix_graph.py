"""Experimental fixed-shape CUDA capture of the unmodified eager prefix."""
import torch


def _clone(value):
    if isinstance(value, torch.Tensor):
        return value.clone()
    if isinstance(value, dict):
        return {key: _clone(item) for key, item in value.items()}
    if value is None:
        return None
    raise TypeError(type(value))


def _copy(target, source):
    if isinstance(target, torch.Tensor):
        if (not isinstance(source, torch.Tensor) or target.shape != source.shape
                or target.dtype != source.dtype or target.device != source.device):
            raise ValueError('Prefix graph tensor contract changed')
        target.copy_(source)
    elif isinstance(target, dict):
        if not isinstance(source, dict) or target.keys() != source.keys():
            raise ValueError('Prefix graph field contract changed')
        for key in target:
            _copy(target[key], source[key])
    elif target is not None or source is not None:
        raise ValueError('Prefix graph optional field changed')


class EagerPrefixGraph:
    def __init__(self, prefix):
        self.prefix = prefix
        self.graph = None

    def __call__(self, observation):
        # The service's separate sync warmup uses inference tensors.
        if torch.is_inference_mode_enabled():
            return self.prefix(observation)
        if self.graph is None:
            self.observation = type(observation)(**_clone(vars(observation)))
            stream = torch.cuda.Stream()
            stream.wait_stream(torch.cuda.current_stream())
            with torch.cuda.stream(stream):
                for _ in range(3):
                    self.prefix(self.observation)
            torch.cuda.current_stream().wait_stream(stream)
            self.graph = torch.cuda.CUDAGraph()
            with torch.cuda.graph(self.graph):
                self.output = self.prefix(self.observation)
        else:
            _copy(vars(self.observation), vars(observation))
        self.graph.replay()
        return self.output

"""Train-only affine input conditioning, folded into unchanged serving layers."""
from __future__ import annotations
import torch


def first_linear(model):
    return next(m for m in model.backbone.modules() if isinstance(m, torch.nn.Linear))


def fit_conditioning(model, observations):
    with torch.no_grad():
        normal = model._actor_state(observations)
        dropped = model._actor_state(observations, apply_reference_dropout=True, reference_dropout_prob=1.)
        values = torch.cat((normal, dropped))
        return dict(mean=values.mean(0).tolist(), scale=values.std(0, unbiased=False).clamp(min=.1).tolist(),
                    source='training_observations_only_with_reference_dropout')


def install_conditioning(model, stats):
    layer = first_linear(model)
    mean = torch.tensor(stats['mean'], device=layer.weight.device, dtype=layer.weight.dtype)
    scale = torch.tensor(stats['scale'], device=layer.weight.device, dtype=layer.weight.dtype)
    if (mean.shape != (layer.in_features,) or scale.shape != mean.shape or not torch.isfinite(mean).all()
            or not torch.isfinite(scale).all() or (scale <= 0).any() or hasattr(model, '_f1_conditioning')):
        raise ValueError('Invalid or repeated actor input conditioning')
    # W'((x-m)/s)+b' is initially exactly the original Wx+b (up to FP32 rounding).
    with torch.no_grad():
        layer.bias.add_(layer.weight @ mean)
        layer.weight.mul_(scale)
    model._f1_conditioning = (mean, scale)
    layer.register_forward_pre_hook(lambda module, args: ((args[0]-mean)/scale,))


def export_actor_state(model):
    weights = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()
               if k.startswith(('backbone.', 'actor_mean.'))}
    if hasattr(model, '_f1_conditioning'):
        mean, scale = (v.detach().cpu() for v in model._f1_conditioning)
        layer = first_linear(model)
        name = next(n for n, module in model.named_modules() if module is layer)
        weights[name+'.weight'].div_(scale)
        weights[name+'.bias'].sub_(weights[name+'.weight'] @ mean)
    return weights

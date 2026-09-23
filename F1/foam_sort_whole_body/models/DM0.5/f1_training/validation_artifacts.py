"""Checkpoint/report provenance and exclusive, atomic JSON publication."""
import json
import os
from pathlib import Path
import tempfile
from f1_data.reader import require, sha256


def write_new(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        # An interrupted writer never leaves a partial report at the final name.
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def checkpoint_identity(checkpoint):
    checkpoint = Path(checkpoint).resolve()
    names = ['adapter_config.json', 'f1_run_binding.json', 'norm_stats.json']
    weights = sorted(checkpoint.glob('adapter_model*.safetensors')) + sorted(checkpoint.glob('adapter_model*.bin'))
    require(weights, 'checkpoint has no adapter weights')
    names += [p.name for p in weights]
    names += [p.name for p in checkpoint.glob('adapter_model*.index.json')]
    return {'checkpoint': str(checkpoint), 'files': {name: sha256(checkpoint/name) for name in names},
            'binding': json.loads((checkpoint/'f1_run_binding.json').read_text())}


def validate_checkpoint(checkpoint, release):
    from .experiment import RECIPE_PATH, code_fingerprints
    identity = checkpoint_identity(checkpoint)
    binding = identity['binding']
    require(binding['release_sha256'] == sha256(release.path), 'checkpoint/release mismatch')
    require(binding['statistics_sha256'] == sha256(release.stats_path) == identity['files']['norm_stats.json'],
            'checkpoint/statistics mismatch')
    require(binding['recipe_sha256'] == sha256(RECIPE_PATH), 'checkpoint/current recipe mismatch')
    require(binding['code'] == code_fingerprints(), 'checkpoint/current code mismatch')
    base = Path(binding['base_model']).resolve()
    adapter = json.loads((Path(checkpoint)/'adapter_config.json').read_text())
    require(Path(adapter['base_model_name_or_path']).resolve() == base, 'adapter/binding base mismatch')
    require(sha256(base/'config.json') == binding['base_config_sha256'], 'base config changed')
    return identity


def load_report(path):
    path = Path(path).resolve()
    report = json.loads(path.read_text())
    require(report.get('schema') == 'f1_generated_action_metrics_v2', 'unsupported report schema')
    require(report.get('checkpoint_identity'), 'report has no checkpoint identity')
    for filename, digest in report.get('prediction_files', {}).items():
        require(Path(filename).name == filename, 'invalid prediction filename')
        require(sha256(path.parent/filename) == digest, 'prediction file changed')
    require(report.get('prediction_files'), 'report has no prediction artifacts')
    return report

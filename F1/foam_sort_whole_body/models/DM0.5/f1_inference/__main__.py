"""6000D entry point; inspect is CPU-only and accepts an empty checkpoint slot."""
import argparse
import json

from . import CONTRACT
from .policy import DEFAULT_CONFIG, load_binding, load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('inspect', 'serve'))
    parser.add_argument('--config', default=DEFAULT_CONFIG)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.command == 'serve' and not config.get('checkpoint'):
            raise ValueError('checkpoint is empty; fill the foam checkpoint slot before serve')
        normalizer, binding = load_binding(config)
        if args.command == 'inspect':
            print(json.dumps({'contract': CONTRACT, 'ready': False, 'model_loaded': False,
                              'checkpoint_configured': bool(config.get('checkpoint')),
                              'reason': 'model_not_loaded' if config.get('checkpoint') else 'checkpoint_not_selected',
                              'model_binding': binding, 'server': config.get('server', {})}, ensure_ascii=False, indent=2))
            return
        from .policy import DM05Policy
        from .server import create_app
        policy = DM05Policy(config, normalizer, binding)
        settings = config.get('server', {})
        app = create_app(policy, binding, max_request_bytes=settings.get('max_request_bytes', 16777216))
        app.run(host=settings.get('host', '127.0.0.1'), port=settings.get('port', 7892),
                threaded=True, debug=False, use_reloader=False)
    except (ValueError, OSError) as exc:
        parser.exit(2, f'F1 inference: {exc}\n')


if __name__ == '__main__':
    main()

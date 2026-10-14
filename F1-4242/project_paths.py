"""Standalone installation paths shared by the gateway and its child processes."""
from pathlib import Path
import json
import os
import string

ROOT = Path(__file__).resolve().parent


def installation_environment(runtime=None):
    home = Path(os.environ.get('F1_INFRA_HOME', str(Path.home() / 'Bruno/F1-infra'))).expanduser().resolve()
    run = Path(runtime or os.environ.get('F1_CONSOLE_RUNTIME', str(home / 'runtime'))).expanduser().resolve()
    local_profile = ROOT / 'configs/robot.json'
    defaults = {
        'F1_INFRA_HOME': str(home),
        'F1_CONSOLE_RUNTIME': str(run),
        'F1_PROFILE_PATH': str(local_profile if local_profile.exists() else ROOT / 'configs/robot.example.json'),
        'F1_CONTROL_LOCK': str(run / 'control.lock'),
        'PICO_UPPER_ROOT': str(ROOT / 'integrations/pico_upper'),
        'PICO_UPPER_RUNTIME': str(run / 'pico_upper'),
        'F1_LXL_ROOT': '/home/user/lxl',
        'F1_CONSOLE_URL': 'http://127.0.0.1:4242',
        'F1_ENV_LOADED': '1',
    }
    values = {key: os.environ.get(key, value) for key, value in defaults.items()}
    if runtime is not None:
        values['F1_CONSOLE_RUNTIME'] = str(run)
    values['PICO_UPPER_CONTROL_LOCK'] = values['F1_CONTROL_LOCK']
    for key in ('PICO_UPPER_PYTHON', 'PICO_UPPER_SDK_ROOT', 'PICO_UPPER_ROS_SETUP',
                'F1_ROS_SETUP', 'F1_ROBOT_SETUP', 'F1_AGV_SETUP', 'F1_CYCLONEDDS_URI',
                'F1_CONSOLE_PYTHON', 'F1_RECORDING_PYTHON', 'PICO_UPPER_CMEEL_LIB',
                'PICO_UPPER_SDK_PYTHON_PATH', 'PICO_UPPER_SDK_LIB',
                'RMW_IMPLEMENTATION', 'ROS_DOMAIN_ID', 'CYCLONEDDS_URI',
                'ROS_AUTOMATIC_DISCOVERY_RANGE', 'F1_CONSOLE_PORT'):
        if key in os.environ:
            values[key] = os.environ[key]
    return values


def expand_path(value, runtime=None):
    """Only expand the documented installation variables, never shell commands."""
    return str(Path(string.Template(str(value)).substitute(installation_environment(runtime))).expanduser())


def profile_path():
    return Path(installation_environment()['F1_PROFILE_PATH']).expanduser().resolve()


def read_profile(path=None):
    profile = json.loads(Path(path or profile_path()).read_text())
    for key in ('data_path', 'command_monitor_binary', 'startup_script', 'ec_startup_script'):
        if profile.get(key):
            profile[key] = expand_path(profile[key])
    if profile.get('full_home_source', {}).get('path'):
        profile['full_home_source']['path'] = expand_path(profile['full_home_source']['path'])
    return profile


def teleop_config(path=None, runtime=None):
    config = json.loads(Path(path or ROOT / 'teleop/config.json').read_text())
    for variant in config.values():
        for key in ('cwd', 'start', 'original_stop'):
            if key in variant:
                variant[key] = expand_path(variant[key], runtime)
    return config

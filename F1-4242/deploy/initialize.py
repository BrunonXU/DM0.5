#!/usr/bin/env python3
"""Prepare a new F1-infra installation using four user choices; never start ROS.

An imported inventory is simulation evidence only. Existing installations must
be upgraded separately: this entry point never overwrites local configuration.
"""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
SCHEMA = 'f1_setup_inventory_v1'
SETUPS = {'ros_setup': 'F1_ROS_SETUP', 'f1_setup': 'F1_ROBOT_SETUP', 'agv_setup': 'F1_AGV_SETUP'}


class SetupError(ValueError):
    pass


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def validate_name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise SetupError('机器人名字须为 1–80 个可显示字符。')
    return value.strip()


def validate_ip(value):
    try:
        ip = ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, TypeError):
        raise SetupError('请输入有效的机器人 IPv4 地址。') from None
    if ip.is_unspecified or ip.is_multicast or int(ip) == 0xffffffff:
        raise SetupError('请选择可访问的单播 IPv4 地址。')
    return str(ip)


def parse_modules(value):
    if not isinstance(value, str):
        raise SetupError('模块只能选择 none、pico、dagger 或 pico,dagger。')
    selected = set(value.lower().replace(' ', '').split(','))
    if selected == {'none'}:
        return []
    if not selected or not selected <= {'pico', 'dagger'}:
        raise SetupError('模块只能选择 none、pico、dagger 或 pico,dagger。')
    return sorted(selected)


def absolute_path(value, home=None):
    value = str(value)
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise SetupError('目录不可为空或包含控制字符。')
    if value == '~' or value.startswith('~/'):
        value = str(Path(home or Path.home()) / value[2:]) if value != '~' else str(home or Path.home())
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or path == Path('/'):
        raise SetupError('请使用绝对目录（可用 ~/），不可使用根目录或 ..。')
    return path


def reject_symlinks(path):
    for item in [path, *path.parents]:
        if item.is_symlink():
            raise SetupError(f'拒绝符号链接路径：{item}')
        if item.exists() and item != path and not item.is_dir():
            raise SetupError(f'路径父级不是目录：{item}')


def existing_parent(path):
    while not path.exists():
        path = path.parent
    return path


def disk_info(path):
    parent = existing_parent(Path(path))
    usage = shutil.disk_usage(parent)
    return {'path': str(parent), 'free_bytes': usage.free, 'total_bytes': usage.total}


def strings(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return sorted(set(x for x in value if x))
    raise SetupError('环境清单路径字段必须为字符串或字符串数组。')


def static_installation(path, home):
    """Read literal assignments only; never evaluate shell syntax or run source."""
    known = {'HOME': str(home), 'HERE': str(path.parent.parent)}
    unresolved = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}, [str(path)]
    for line in lines:
        match = re.match(r'^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=(.*)$', line)
        if not match:
            continue
        key, value = match.groups()
        try:
            words = shlex.split(value, comments=True)
        except ValueError:
            words = []
        if len(words) != 1 or '`' in value or '$(' in value:
            if key in ('F1_CONTROL_LOCK', 'PICO_UPPER_CONTROL_LOCK'):
                unresolved.append(str(path))
            continue
        def replace(m):
            return known.get(m.group(1) or m.group(2), m.group(0))
        resolved = re.sub(r'\$\{([A-Z][A-Z0-9_]*)\}|\$([A-Z][A-Z0-9_]*)', replace, words[0])
        if '$' in resolved:
            if key in ('F1_CONTROL_LOCK', 'PICO_UPPER_CONTROL_LOCK'):
                unresolved.append(str(path))
            continue
        known[key] = resolved
    return known, unresolved



def process_environments():
    """Read only a strict allowlist from this account's running F1 servers."""
    keys = {'F1_INFRA_HOME', 'F1_CONSOLE_RUNTIME', 'F1_CONTROL_LOCK',
            'PICO_UPPER_CONTROL_LOCK', 'F1_ROS_SETUP', 'F1_ROBOT_SETUP',
            'F1_AGV_SETUP', 'F1_CYCLONEDDS_URI', 'CYCLONEDDS_URI',
            'RMW_IMPLEMENTATION', 'ROS_DOMAIN_ID', 'F1_INSTALLATION_ENV',
            'PICO_UPPER_PYTHON', 'PICO_UPPER_SDK_ROOT', 'F1_RECORDING_PYTHON'}
    found = []
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            argv = (proc / 'cmdline').read_bytes().split(b'\0')
            if not any(Path(arg.decode(errors='replace')).name == 'server.py' for arg in argv):
                continue
            values = {}
            for entry in (proc / 'environ').read_bytes().split(b'\0'):
                key, sep, value = entry.partition(b'=')
                key = key.decode(errors='replace')
                if sep and key in keys:
                    values[key] = value.decode(errors='replace')
            if values.get('F1_CONSOLE_RUNTIME') and values.get('F1_INFRA_HOME'):
                found.append(values)
        except (OSError, ValueError):
            continue
    return found

def probe(output_dir=None):
    home = Path.home()
    root = Path(output_dir or SOURCE)
    addresses = []
    notes = []
    running = process_environments()
    observed = {}
    for values in [*running, dict(os.environ)]:
        for key, value in values.items():
            if value:
                observed.setdefault(key, set()).add(value)
    conflicts = sorted(key for key in ('F1_CYCLONEDDS_URI', 'CYCLONEDDS_URI', 'RMW_IMPLEMENTATION', 'ROS_DOMAIN_ID') if len(observed.get(key, set())) > 1)
    def unique_env(key, default=''):
        values = observed.get(key, set())
        return next(iter(values)) if len(values) == 1 else default
    try:
        result = subprocess.run(['ip', '-j', '-4', 'addr', 'show'], capture_output=True, text=True, timeout=5, check=True)
        for interface in json.loads(result.stdout):
            for addr in interface.get('addr_info', []):
                if addr.get('family') == 'inet':
                    value = validate_ip(addr['local'])
                    if not ipaddress.IPv4Address(value).is_loopback:
                        addresses.append({'address': value, 'interface': interface.get('ifname', '')})
    except (OSError, ValueError, subprocess.SubprocessError):
        notes.append('无法读取本机 IPv4 列表，请手工填写机器人地址。')
    try:
        machine_id = Path('/etc/machine-id').read_text().strip()
    except OSError:
        machine_id = ''
    setups = {}
    for field, env in SETUPS.items():
        candidates = list(observed.get(env, set()))
        if field == 'ros_setup':
            candidates += ['/opt/ros/jazzy/setup.bash']
        else:
            word = 'f1' if field == 'f1_setup' else 'agv'
            candidates += [str(home / 'workspace' / word / 'install/setup.bash')]
            workspace = home / 'workspace'
            if workspace.is_dir():
                candidates += [str(p) for p in workspace.glob('*/install/setup.bash') if word in p.parts[-3].lower()]
        setups[field] = sorted({str(Path(x).expanduser().resolve()) for x in candidates if Path(x).expanduser().is_file()})
    locks = set()
    for key in ('F1_CONTROL_LOCK', 'PICO_UPPER_CONTROL_LOCK'):
        for value in observed.get(key, set()):
            locks.add(str(absolute_path(value, home).resolve()))
    for values in running:
        if not values.get('F1_CONTROL_LOCK') and not values.get('PICO_UPPER_CONTROL_LOCK'):
            locks.add(str(absolute_path(values['F1_CONSOLE_RUNTIME'], home) / 'control.lock'))
    unresolved = []
    for source in observed.get('F1_INSTALLATION_ENV', set()):
        values, issues = static_installation(Path(source), home)
        unresolved += issues
        for key in ('F1_CONTROL_LOCK', 'PICO_UPPER_CONTROL_LOCK'):
            if values.get(key):
                locks.add(str(absolute_path(values[key], home).resolve()))
    roots = {root, home / 'Bruno/F1-infra', home / 'F1-infra'}
    # Compatibility probe only: the old F1 lock must stay shared on F1-09.
    for path in [home / 'Bruno/F1/runtime/control.lock', *(r / 'runtime/control.lock' for r in roots)]:
        if path.exists():
            locks.add(str(path.resolve()))
    for r in roots:
        for path in (r / 'configs/installation.env', r / 'current/configs/installation.env'):
            if path.is_file():
                values, issues = static_installation(path, home)
                unresolved += issues
                for key in ('F1_CONTROL_LOCK', 'PICO_UPPER_CONTROL_LOCK'):
                    if values.get(key):
                        locks.add(str(absolute_path(values[key], home).resolve()))
    optional = {}
    for field, env in {'pico_python': 'PICO_UPPER_PYTHON', 'pico_sdk': 'PICO_UPPER_SDK_ROOT', 'recording_python': 'F1_RECORDING_PYTHON', 'dagger_root': 'F1_DAGGER_ROOT'}.items():
        value = unique_env(env)
        if value and Path(value).expanduser().exists():
            optional[field] = str(Path(value).expanduser().resolve())
    if 'pico_python' not in optional:
        p = home / 'miniconda3/envs/pico4ultra_py312/bin/python'
        if p.is_file():
            optional['pico_python'] = str(p)
    dds = unique_env('F1_CYCLONEDDS_URI', unique_env('CYCLONEDDS_URI'))
    if not dds and Path('/etc/ros/cyclonedds/cyclonedds.xml').is_file():
        dds = '/etc/ros/cyclonedds/cyclonedds.xml'
    return dict(schema=SCHEMA, account={'home': str(home), 'username': getpass.getuser()},
                architecture=platform.machine(), machine_id=machine_id, addresses=addresses,
                **setups, dds={'domain_id': unique_env('ROS_DOMAIN_ID', '0'), 'rmw': unique_env('RMW_IMPLEMENTATION', 'rmw_cyclonedds_cpp'), 'config': dds}, process_environment_conflicts=conflicts,
                lock_candidates=sorted(locks), unresolved_lock_sources=sorted(set(unresolved)), optional_paths=optional,
                python={'executable': sys.executable, 'version': platform.python_version()}, disk=disk_info(root), notes=notes)


def load_inventory(path):
    source = Path(path)
    if source.is_symlink() or source.stat().st_size > 1024 * 1024:
        raise SetupError('环境清单必须是小于 1 MiB 的普通 JSON 文件。')
    value = json.loads(source.read_text())
    if not isinstance(value, dict) or value.get('schema') != SCHEMA:
        raise SetupError('环境清单 schema 应为 f1_setup_inventory_v1。')
    return value


def ip_choices(inventory):
    choices = []
    for item in inventory.get('addresses', []):
        value = item if isinstance(item, str) else item.get('address', item.get('local', ''))
        ip = validate_ip(value)
        if not ipaddress.IPv4Address(ip).is_loopback and ip not in choices:
            choices.append(ip)
    return choices


def choose_inputs(args, inventory):
    interactive = sys.stdin.isatty()
    home = inventory.get('account', {}).get('home', str(Path.home()))
    name = args.name
    if name is None:
        if not interactive:
            raise SetupError('非交互运行请提供 --name。')
        name = input('机器人名字（例如 F1-09）：')
    choices = ip_choices(inventory)
    ip = args.ip
    if ip is None:
        if not interactive:
            if len(choices) != 1:
                raise SetupError('检测到多个或零个 IPv4 地址，请用 --ip 明确选择。')
            ip = choices[0]
        else:
            default = choices[0] if len(choices) == 1 else ''
            if choices:
                print('检测到的 IPv4：' + ' / '.join(choices))
            ip = input('电脑可访问的机器人 IP' + (f' [{default}]' if default else '') + '：').strip() or default
    data = args.data_dir
    if data is None:
        default = str(Path(home) / 'F1-data')
        data = (input(f'数据目录 [{default}]：').strip() or default) if interactive else default
    modules = args.modules
    if modules is None:
        modules = (input('接入模块 none / pico / dagger / pico,dagger [none]：').strip() or 'none') if interactive else 'none'
    return validate_name(name), validate_ip(ip), absolute_path(data, home), parse_modules(modules)


def prepare(inventory, *, name, ip, data_dir, modules, output_dir, simulation=False):
    """Return deterministic file contents. No filesystem changes are made here."""
    name, ip = validate_name(name), validate_ip(ip)
    modules = parse_modules(','.join(modules) if modules else 'none')
    home = inventory.get('account', {}).get('home', str(Path.home()))
    output = absolute_path(output_dir)
    data = absolute_path(data_dir, home)
    reject_symlinks(output)
    # Imported remote paths must not be mistaken for local mounted data.
    if not simulation:
        reject_symlinks(data)
        if data.exists() and not data.is_dir():
            raise SetupError('数据目录已经存在且不是目录。')
    machine_id = inventory.get('machine_id', '')
    if not isinstance(machine_id, str) or not re.fullmatch(r'[a-fA-F0-9]{32}', machine_id):
        raise SetupError('未检测到有效 machine-id；请在机器人本机运行初始化。')
    machine_id = machine_id.lower()
    runtime = output / 'runtime'
    pending = []
    locks = strings(inventory.get('lock_candidates', []))
    previous_lock = inventory.get('installation', {}).get('control_lock')
    if previous_lock:
        locks += strings(previous_lock)
    locks = sorted({str(absolute_path(x, home)) for x in locks})
    if inventory.get('process_environment_conflicts'):
        raise SetupError('多个运行环境的 DDS 配置冲突，请部署人员核对：' + ', '.join(inventory['process_environment_conflicts']))
    if inventory.get('unresolved_lock_sources'):
        raise SetupError('已有安装的共享锁无法静态解析；请部署人员明确现有 F1_CONTROL_LOCK 后再运行。')
    if len(locks) > 1:
        raise SetupError('检测到多个共享控制锁，不能另建锁或自动选择：' + ', '.join(locks))
    lock = locks[0] if locks else str(runtime / 'control.lock')
    env = {'F1_INFRA_HOME': str(output), 'F1_CONSOLE_RUNTIME': str(runtime),
           'F1_PROFILE_PATH': str(output / 'configs/robot.json'), 'F1_SITE_PATH': str(output / 'configs/site.json'),
           'F1_CONTROL_LOCK': lock, 'PICO_UPPER_CONTROL_LOCK': lock, 'F1_CONSOLE_PORT': '4242',
           'F1_CONSOLE_PYTHON': inventory.get('python', {}).get('executable') or '/usr/bin/python3',
           'F1_LXL_ROOT': str(output / 'optional/lxl-not-configured')}
    setups_found = True
    for field, key in SETUPS.items():
        candidates = strings(inventory.get(field, []))
        if len(candidates) == 1:
            env[key] = str(absolute_path(candidates[0], home))
        else:
            setups_found = False
            # Explicit empty values prevent unnoticed fallback to another user's path.
            env[key] = ''
            pending.append({'code': field, 'message': '未找到环境路径' if not candidates else '存在多个候选，请部署人员核对', 'candidates': candidates})
    dds = inventory.get('dds', {})
    domain = str(dds.get('domain_id', '0'))
    if not domain.isdigit() or not 0 <= int(domain) <= 232:
        raise SetupError('ROS_DOMAIN_ID 应为 0–232 的整数。')
    env['ROS_DOMAIN_ID'] = domain
    env['RMW_IMPLEMENTATION'] = str(dds.get('rmw') or 'rmw_cyclonedds_cpp')
    dds_config = str(dds.get('config') or '')
    if dds_config:
        env['F1_CYCLONEDDS_URI'] = dds_config
    else:
        env['F1_CYCLONEDDS_URI'] = ''
        pending.append({'code': 'dds_config', 'message': '未找到 DDS 配置，需核对网络发现设置。'})
    python_version = str(inventory.get('python', {}).get('version') or inventory.get('python_version', 'unknown'))
    if not python_version.startswith('3.12.'):
        pending.append({'code': 'python_version', 'message': f'检测到 Python {python_version}；机器人 ROS Jazzy 配套环境需核对 Python 3.12。'})
    pending.append({'code': 'ros_runtime_unverified', 'message': '仅探测环境文件，尚未加载或验证 ROS 消息、驱动和相机。'})
    pending.append({'code': 'motion_acceptance', 'message': '动作保持关闭；本机驱动、监听器、关节和限位需由部署人员验收。'})
    if simulation:
        pending.insert(0, {'code': 'simulation', 'message': '使用导入的清单模拟；文件存在性和环境状态不代表本机已就绪。'})
    if ipaddress.IPv4Address(ip).is_loopback:
        pending.append({'code': 'loopback_ip', 'message': '回环地址只能本机访问，其他电脑请使用机器人网卡 IP。'})
    optional = inventory.get('optional_paths', {})
    module_status = {}
    for module in modules:
        module_status[module] = 'pending_integration'
        pending.append({'code': module, 'message': ('已生成 Pico 30Hz 本地录制配置；Python、SDK 和录制依赖仍需按 Pico 文档检查。' if module == 'pico' else '手柄 DAgger 需另接收端、Windows sender 与数据根配置；本工具不安装或启动它们。')})
    if 'pico' in modules:
        env.update(PICO_UPPER_ROOT=str(output / 'integrations/pico_upper'), PICO_UPPER_RUNTIME=str(runtime / 'pico_upper'))
        for field, key in {'pico_python': 'PICO_UPPER_PYTHON', 'pico_sdk': 'PICO_UPPER_SDK_ROOT', 'recording_python': 'F1_RECORDING_PYTHON'}.items():
            if optional.get(field):
                env[key] = str(absolute_path(optional[field], home))
    profile = json.loads((SOURCE / 'configs/robot.example.json').read_text())
    def disable(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith(('_enabled', '_verified')):
                    value[key] = False
                else:
                    disable(item)
        elif isinstance(value, list):
            for item in value:
                disable(item)
    disable(profile)
    stable_id = 'machine-' + machine_id[:12]
    profile.update(id=stable_id, robot_id=stable_id, label=stable_id, ip='127.0.0.1', expected_machine_id=machine_id, default_presets=[])
    profile.pop('joint_contract', None)
    profile['startup_script'] = ''
    profile['ec_startup_script'] = ''
    storage = {'schema': 'f1_local_storage_v1', 'root': str(data), 'archive': {'enabled': False, 'idle_only': True, 'max_parallel': 1, 'bytes_per_second': 2097152, 'delete_local_after_upload': False}, 'legacy_roots': {}}
    report = {'schema': 'f1_setup_report_v1', 'simulation': simulation, 'site': {'name': name, 'ip': ip, 'port': 4242},
              'machine': {'id': stable_id, 'machine_id': machine_id, 'architecture': inventory.get('architecture', ''), 'account': inventory.get('account', {})},
              'output_dir': str(output), 'data_dir': str(data), 'modules': modules, 'module_status': module_status,
              'control_lock': {'path': lock, 'source': 'existing' if locks else 'new_installation'},
              'web_configured': True, 'ros_files_found': setups_found, 'ros_ready': False, 'motion_enabled': False,
              'python': inventory.get('python', {'version': inventory.get('python_version', 'unknown')}), 'disk': inventory.get('disk', {}) if simulation else disk_info(data), 'pending': pending,
              'effects': {'services_started': False, 'ros_started': False, 'motion_sent': False, 'data_created': False, 'existing_files_overwritten': False}}
    content = '# Generated by deploy/initialize.py. Values are literal shell-quoted strings.\n'
    content += '# Simulation only; do not use as a verified robot installation.\n' if simulation else ''
    content += ''.join(f'export {key}={shlex.quote(str(value))}\n' for key, value in env.items())
    files = {'configs/installation.env': content, 'configs/robot.json': json_text(profile),
             'configs/site.json': json_text({'schema': 'f1_site_v1', 'label': name, 'ip': ip}),
             'runtime/storage.json': json_text(storage), 'runtime/setup-report.json': json_text(report)}
    if 'pico' in modules:
        recording = json.loads((SOURCE / 'configs/recording.unified.example.json').read_text())
        files['runtime/recording.json'] = json_text(recording)
    return files, report


def write_new_installation(root, files):
    """Preflight all destinations, create exclusively, roll back only own writes."""
    root = absolute_path(root)
    reject_symlinks(root)
    targets = [(root / relative, content) for relative, content in files.items()]
    for target, content in targets:
        reject_symlinks(target)
        if target.exists():
            same = target.is_file() and target.read_text() == content
            if not same and target.name == 'setup-report.json' and target.is_file():
                try:
                    old, new = json.loads(target.read_text()), json.loads(content)
                    old.pop('disk', None)
                    new.pop('disk', None)
                    same = old == new
                except (ValueError, AttributeError):
                    pass
            if not same:
                raise SetupError(f'保留已有配置，未写入任何文件：{target}。升级请复用原配置；模拟请使用新的 --output-dir。')
    created = []
    directories = []
    try:
        for target, content in targets:
            if target.exists():
                continue
            missing = []
            parent = target.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for parent in reversed(missing):
                parent.mkdir(mode=0o700)
                directories.append(parent)
            reject_symlinks(target)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            created.append(target)
            with os.fdopen(fd, 'w') as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
    except BaseException:
        for path in reversed(created):
            path.unlink()
        for path in reversed(directories):
            path.rmdir()
        raise
    return [str(path.relative_to(root)) for path in created]


def main(argv=None):
    parser = argparse.ArgumentParser(description='F1-infra 首次配置：名字、IP、数据目录、可选模块。只写新配置，不安装依赖或启动机器人。')
    parser.add_argument('--name')
    parser.add_argument('--ip')
    parser.add_argument('--data-dir')
    parser.add_argument('--modules', help='none / pico / dagger / pico,dagger')
    parser.add_argument('--output-dir', default=str(SOURCE), help='新安装根；模拟时请选择隔离目录')
    parser.add_argument('--inventory', help='只用于模拟的只读环境清单 JSON')
    parser.add_argument('--probe-only', action='store_true', help='仅输出本机环境 JSON，不写文件')
    args = parser.parse_args(argv)
    try:
        if args.probe_only:
            if args.inventory:
                raise SetupError('--probe-only 探测本机，请勿同时传入 --inventory。')
            print(json_text(probe(args.output_dir)), end='')
            return 0
        inventory = load_inventory(args.inventory) if args.inventory else probe(args.output_dir)
        name, ip, data, modules = choose_inputs(args, inventory)
        files, report = prepare(inventory, name=name, ip=ip, data_dir=data, modules=modules, output_dir=args.output_dir, simulation=bool(args.inventory))
        created = write_new_installation(args.output_dir, files)
        print(('模拟配置已保存' if args.inventory else '首次配置已保存') + f'：{args.output_dir}')
        print(f'网页地址：http://{ip}:4242')
        print('本次写入：' + (', '.join(created) if created else '无（内容相同，保留已有文件）'))
        for item in report['pending']:
            print('待核验：' + item['message'])
        print('配置完成不等于机器人就绪；未启动服务、ROS、遥操或动作。')
        return 0
    except (SetupError, OSError, json.JSONDecodeError) as exc:
        print(f'初始化未完成：{exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

"""Read-only HIL overview for an independently owned DAgger process.

v15 already emits F1 STATUS summaries. Read a bounded console tail (never raw
frame/control journals); no ROS imports, launch, restart or command endpoints.
"""
from collections import deque
from pathlib import Path
import copy
import json
import os
import re
import shlex
import threading
import time

MAX_TAIL = 128 * 1024
STALE_S = 8.0  # v15 publishes a summary every 5 s or on a state change.


def small_json(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('HIL configuration exceeds size limit')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('HIL configuration must be an object')
    return value


def processes():
    found = []
    ticks = os.sysconf('SC_CLK_TCK')
    boot = next(float(row.split()[1]) for row in Path('/proc/stat').read_text().splitlines() if row.startswith('btime '))
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            args = (path/'cmdline').read_bytes().split(b'\0')
            if not any(args[i:i+2] == [b'-m', b'f1dagger'] for i in range(len(args)-1)):
                continue
            if not any(mode in args for mode in (b'teleop', b'run', b'shadow', b'policy-shadow')):
                continue
            stat = (path/'stat').read_text().rsplit(')', 1)[1].split()
            if stat[0] == 'Z':
                continue
            runtime = None
            if b'--runtime' in args:
                runtime = args[args.index(b'--runtime')+1].decode()
            found.append(dict(pid=int(path.name), start_ticks=stat[19], started_unix=boot+int(stat[19])/ticks,
                              runtime=runtime))
        except (OSError, ValueError, IndexError):
            continue
    return found


def parse_tail(text):
    """Reset at each startup identity so a new process cannot inherit old green."""
    result = dict(identity={}, status=None, run=None, failure=None, phase='initializing', status_offset=None)
    offset=0
    for raw_line in text.splitlines(keepends=True):
        line=raw_line.rstrip('\r\n')
        line_offset=offset
        offset+=len(raw_line.encode('utf-8'))
        if line.startswith('F1 IDENTITY '):
            result = dict(identity={}, status=None, run=None, failure=None, phase='initializing', status_offset=None)
            try:
                identity = json.loads(line[len('F1 IDENTITY '):])
                result['identity'] = identity if isinstance(identity,dict) else {}
            except ValueError:
                pass
        if 'local operator ready. Press Enter' in line:
            result['phase'] = 'awaiting_enter'
        if re.match(r'F1 (POWERED|SHADOW) \(', line) and '; raw=' in line:
            result['run'] = line.rsplit('; raw=', 1)[1].strip()
            result['phase'] = 'waiting_for_teleop_start'
        if line.startswith('F1 STATUS '):
            try:
                data = json.loads(line[len('F1 STATUS '):])
                if isinstance(data, dict):
                    result['status'] = data
                    result['status_offset'] = line_offset
                    result['phase'] = data.get('state', 'unknown')
            except ValueError:
                pass
        if line.startswith(('F1 FAILED:', 'RuntimeError:', 'ValueError:', 'F1 cleanup incomplete:')):
            result['failure'] = line[:600]
    return result


def guidance(phase, status):
    if phase == 'episode_sender_update_required' or status.get('hold_reason') == 'episode_sender_update_required':
        return 'START已收到，但当前sender缺少连续分条协议。请停止Windows sender，执行update_episodes.ps1，看到连续分条更新成功后重新启动sender和F1入口；反复按START无效。'
    if phase == 'episode_ready': return '当前未录制；整理现场后释放控制，再按 START 开始新的一条。'
    if phase == 'episode_starting': return '正在准备记录；保持控制释放。'
    if phase == 'episode_saving': return '正在保存本条；完成后再按 START。提前按键不会排队。'
    recovery = {
        'input_wait_reconnect': '等待 sender 和手柄重新连接；重连后松 RB、松开其他按键并让摇杆回中。',
        'input_wait_release_rb': '松 RB、松开其他按键并让摇杆回中，等待提示后再按 RB。',
        'input_wait_fresh_packet': '保持松 RB、摇杆回中，等待新鲜输入确认。',
        'input_wait_press_rb': '输入已确认；重新按 RB，再换安全方向移动。',
        'input_wait_feedback': '本次 RB 未恢复控制；松 RB、摇杆回中，等机器人反馈恢复后再按 RB。',
        'input_wait_safe_anchor': '当前实测姿态未通过检查；先松 RB、摇杆回中并排查原因，确认正常后再按 RB。',
        'input_wait_update_sender': '更新 Windows sender；重连后松 RB、摇杆回中，再按 RB。',
    }
    if phase in recovery:
        cause = str(status.get('recovery_reason') or '')
        prefix = ('该方向有碰撞风险，候选动作已拦住。' if cause.startswith('manual_candidate_collision') else
                  '手柄已断开，已暂停控制。' if cause == 'controller_disconnected' else '')
        recording = ('自由接入不录制。' if status.get('purpose') == 'teleop_play' else
                     '同一条仍在录制；暂停区间会单独标记。' if status.get('recording', {}).get('phase') == 'recording' else '')
        return prefix + recovery[phase] + recording + '不要按 START 恢复。'
    if phase == 'not_configured': return '尚未配置 HIL 入口，请先完成本机安装配置。'
    if phase == 'idle': return '展开启动指引，按顺序打开三个终端。'
    if phase == 'failed': return '运行端报告异常；查看最近消息及 F1 终端，排除原因后再启动。'
    if phase == 'unknown': return '状态摘要已过期或无法确认；先查看 F1 运行终端，不要重复启动。'
    if phase == 'conflict': return '发现多个 HIL 接收程序；先在原终端核对会话。'
    if phase == 'initializing': return 'F1 正在初始化，等待运行终端提示。'
    if phase == 'awaiting_enter': return '在 F1 现场终端按 Enter，然后等待 sender 模式回执。'
    if phase in ('waiting_for_start', 'waiting_for_teleop_start'):
        if not status.get('input_fresh'): return '等待 sender 数据；依次检查 Windows 隧道和 sender。'
        return '释放控制后单击 START，'+('进入人工控制。' if phase.endswith('teleop_start') else '开始策略流程。')
    if phase == 'safe_hold':
        record_phase = status.get('recording', {}).get('phase')
        if record_phase == 'recording' and str(status.get('hold_reason') or '').startswith(('ik_rejected:', 'policy_failed:', 'rtc_')):
            return '策略已停止，本条仍在录制。释放控制后按 START 转人工接管，再按 RB 操作；完成后按↑保存。'
        recording = ('本条因保护停止正在封存。' if record_phase == 'saving' else
                     '当前未录制；保护停止不会续录旧条。' if record_phase == 'idle' else
                     '请查看运行终端确认本条状态；必要时结束运行端。')
        return recording + '先松 RB，按原始原因排障；不要反复按 START。确认问题已排除且保存完成后，START 只会开新条。'
    if phase in ('handoff_hold', 'manual_hold'): return '正在保持；等待交接条件完成，以 sender 回执为准。'
    if phase == 'policy': return '策略运行中；按 START 请求人工接管。'
    if phase == 'hil_latched':
        if not status.get('side'): return '松开 RB、摇杆回中，再按住 RB 控制右臂；无需十字键选臂。'
        return '按住 RB 并推动摇杆控制右臂；RT 切换右夹爪。' if not status.get('rb') else '正在请求控臂；松 RB 保持，仍留在人工模式。'
    return '查看 sender 回执与最近消息，确认当前阶段。'


class HILService:
    def __init__(self, runtime, *, offline=False, clock=time.time, process_reader=processes):
        self.runtime = Path(runtime)
        self.probe_lock = threading.Lock()
        self.policy_check = None
        self.process_cache = (0., [])
        self.config_path = Path(os.environ.get('F1_HIL_CONFIG', str(Path(runtime)/'hil.json')))
        self.clock, self.process_reader, self.offline = clock, process_reader, offline
        self.lock = threading.Lock()
        self.cached, self.cached_at = None, -float('inf')
        self.events = deque(maxlen=12)
        self.signature = None
        self.sample_key, self.sample_at = None, 0.

    def commands(self, config):
        host = config['ssh_host']
        if not re.fullmatch(r'[A-Za-z0-9_.-]+@[A-Za-z0-9.-]+', host):
            raise ValueError('invalid HIL SSH destination')
        launcher = str(Path(config['launcher']).expanduser())
        return [dict(id='robot', title='1 · F1 运行端', where='F1 现场终端', command='bash '+shlex.quote(launcher), hint='基础启动完成后执行；按 Enter，等待 START。'),
            dict(id='tunnel', title='2 · Windows 隧道', where='新开 PowerShell，窗口保持打开', command='ssh -F NUL -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -L 127.0.0.1:7907:127.0.0.1:7906 '+host, hint='连接后无输出可以是正常状态；先不要关闭窗口。'),
            dict(id='sender', title='3 · Windows sender', where='另开 PowerShell', command='cd "$HOME/DAgger-F1"\n.\\scripts\\windows_f1_sender.cmd', hint='收到 F1 模式回执后再按 START。F710 使用 X 模式，左摇杆应控制 X/Y 轴。')]

    def saved_identity(self, saved):
        if not isinstance(saved, dict) or saved.get('phase') != 'saved':
            return saved
        from hil_data import find_run, HILData
        enriched = dict(saved)
        try:
            run = find_run(self, saved['run_id'])
            row = HILData(self).row(run)
            if row and row['recording_status'] == 'committed':
                enriched['data_episode_id'] = row['id']
                process = row.get('binding', {}).get('process_run_id')
                if process: enriched['process_run_id'] = process
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return enriched

    def receiver_runtime(self, default, active):
        """Only installed runtime allowlist entries may supply status/finish files."""
        roots = {default}
        try:
            installation = small_json(self.runtime/'hil-routes.json')
            routes = installation.get('routes', {})
            roots.update(Path(item['runtime']).expanduser().resolve()
                         for item in routes.values() if item.get('runtime'))
            models = installation.get('installed_policy_models', {})
            if not isinstance(models, dict):
                raise ValueError('Installed policy models must be an object')
            for item in models.values():
                if (not isinstance(item, dict)
                        or not isinstance(item.get('model_id'), str) or not item['model_id'].strip()
                        or not isinstance(item.get('runtime'), str) or not item['runtime'].strip()):
                    raise ValueError('Installed policy model requires model_id and runtime')
                root = Path(item['runtime']).expanduser()
                if not root.is_absolute():
                    raise ValueError('Installed policy model runtime must be absolute')
                roots.add(root.resolve())
        except FileNotFoundError:
            pass
        if len(active) == 1 and active[0].get('runtime'):
            candidate = Path(active[0]['runtime']).expanduser().resolve()
            if candidate not in roots:
                raise ValueError('Active HIL runtime is not installed in route registry')
            return candidate
        if not active:
            saved = [root for root in roots if (root/'last-session.json').is_file()]
            if saved:
                return max(saved, key=lambda root: (root/'last-session.json').stat().st_mtime_ns)
        return default

    def state(self):
        with self.lock:
            now = self.clock()
            if self.cached is not None and 0 <= now-self.cached_at < .2:
                return copy.deepcopy(self.cached)
            result = dict(configured=False, phase='not_configured', active=False, summary_fresh=False,
                          commands=[], status={}, events=[], source='receiver_console_summary', checked_unix=now)
            try:
                config = small_json(self.config_path)
                if config.get('schema') != 'f1_hil_panel_v1': raise ValueError('unsupported HIL configuration')
                result.update(configured=True, commands=self.commands(config), phase='idle')
                root = Path(config['runtime']).expanduser().resolve()
                log = root/'onsite-powered-console.log'
                result['log_path'] = str(log)
                if self.offline:
                    active=[]
                elif self.process_reader is not processes:
                    active=self.process_reader()
                else:
                    checked,active=self.process_cache
                    if now-checked>=1 or now<checked:
                        active=processes();self.process_cache=(now,active)
                    # Cheap identity validation between full /proc discoveries.
                    current=[]
                    for process in active:
                        try:
                            stat=(Path('/proc')/str(process['pid'])/'stat').read_text().rsplit(')',1)[1].split()
                            if stat[0]!='Z' and stat[19]==process['start_ticks']:current.append(process)
                        except (OSError,IndexError):pass
                    active=current
                result['active'] = bool(active)
                root = self.receiver_runtime(root, active)
                log = root/'onsite-powered-console.log'
                result.update(log_path=str(log), receiver_runtime=str(root))
                if len(active) > 1: result['phase'] = 'conflict'
                elif active:
                    result.update(phase='unknown', pid=active[0]['pid'], started_unix=active[0]['started_unix'])
                try:
                    with log.open('rb') as stream:
                        stat = os.fstat(stream.fileno())
                        stream.seek(max(0, stat.st_size-MAX_TAIL))
                        raw = stream.read(MAX_TAIL)
                    # Drop a partial first line when seeking into a large file.
                    if stat.st_size > MAX_TAIL: raw = raw.partition(b'\n')[2]
                    parsed = parse_tail(raw.decode('utf-8', errors='replace'))
                    age = now-stat.st_mtime
                    result['summary_age_s'] = max(0., age)
                    result['last_failure'] = parsed['failure']
                    identity = parsed['identity']
                    result['release'] = identity.get('release')
                    if parsed['run']:
                        run = Path(parsed['run']).resolve()
                        if run.parent == root/'raw': result['run_id'] = run.name
                    # A previous process, old boot or unchanged historical log is never live.
                    if len(active) == 1 and active[0]['started_unix'] <= stat.st_mtime <= now+1:
                        if parsed['status'] is not None:
                            key=(active[0]['pid'],active[0]['start_ticks'],stat.st_ino,stat.st_size-len(raw)+parsed['status_offset'])
                            if key != self.sample_key:
                                self.sample_key,self.sample_at=key,min(now,stat.st_mtime)
                            age=now-self.sample_at
                            result['summary_age_s']=max(0.,age)
                        if age <= STALE_S:
                            data = parsed['status'] or {}
                            allowed = ('execution','state','hold_reason','input_fresh','side','rb','home','warning','start_toggle','packet_seq','packet_age_ms','observation_id','observation_age_ms','camera_age_ms','recording','purpose','input_paused','recovery_reason','recovery_required','recovery_error')
                            result['status'] = {k:data[k] for k in allowed if k in data}
                            result.update(phase=parsed['phase'], summary_fresh=bool(data))
                            if parsed['failure']:
                                result.update(phase='failed', summary_fresh=False, status={})
                        elif parsed['phase'] == 'awaiting_enter' and not parsed['status']:
                            result['phase'] = 'awaiting_enter'
                except FileNotFoundError:
                    pass
                snapshot_path=root/'status.json'
                if snapshot_path.exists():
                    snapshot=small_json(snapshot_path)
                    producer_same=(len(active)==1 and snapshot.get('pid')==active[0]['pid']
                                   and snapshot.get('start_ticks')==active[0]['start_ticks'])
                    # A stale snapshot from a previous v16 session must not hide
                    # a fresh, process-bound v15 console after an explicit rollback.
                    # The current structured producer still fails closed on expiry.
                    legacy_current=result['summary_fresh'] and not producer_same
                    if not legacy_current:
                        result.update(source='structured_snapshot',status={},summary_fresh=False,
                                      phase=(result['phase'] if result['phase'] in ('awaiting_enter','initializing') else 'unknown') if active else 'idle')
                        age=now-snapshot.get('updated_unix',0)
                        same=producer_same and snapshot.get('updated_unix',0)>=active[0]['started_unix']
                        if snapshot.get('schema')=='f1_hil_status_v1' and same and 0<=age<=2:
                            data=snapshot['status']
                            result.update(status=data,summary_fresh=True,summary_age_s=age,
                                          phase=data['state'],run_id=snapshot['run_id'],binding=snapshot['binding'],
                                          session_key={k:snapshot[k] for k in ('run_id','pid','start_ticks')},
                                          can_finish='finish_session_v1' in snapshot.get('capabilities',[]))
            except FileNotFoundError:
                pass
            except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
                result.update(phase='unknown', error=str(exc)[:300])
            try:
                completion=self.saved_identity(small_json(root/'last-session.json'))
                if completion.get('schema')=='f1_session_completion_v1' and (not result['active'] or completion.get('run_id')==result.get('run_id') or completion.get('process_run_id')==result.get('run_id')):
                    if completion.get('phase')=='finalizing' and not result['active']:
                        completion=dict(completion,phase='interrupted',errors=['收尾进程已退出；请检查原始文件'])
                    if completion.get('phase')=='saved':
                        from hil_data import find_run, episode_id
                        saved_run=find_run(self,completion['run_id'])
                        completion['episode_id']=episode_id(saved_run)
                    result['completion']=completion
                    if result['active'] and completion.get('phase')=='finalizing':result['can_finish']=False
            except (OSError,ValueError,UnboundLocalError):pass
            result['status_ttl_s'] = 2 if result.get('source') == 'structured_snapshot' else STALE_S
            record = result['status'].get('recording')
            if isinstance(record, dict) and record.get('last_saved'):
                record['last_saved'] = self.saved_identity(record['last_saved'])
            data = result['status']
            if result['active'] and result['summary_fresh'] and data.get('hold_reason') == 'episode_sender_update_required':
                result['phase'] = 'episode_sender_update_required'
            result['next_step'] = guidance(result['phase'], data)
            signature = (result.get('pid'), result['phase'], data.get('hold_reason'), data.get('warning'))
            if signature != self.signature:
                self.signature = signature
                self.events.appendleft(dict(time=now, phase=result['phase'], reason=data.get('hold_reason') or data.get('warning'), message=result['next_step']))
            result['events'] = list(self.events)[:5]
            self.cached, self.cached_at = result, now
            return copy.deepcopy(result)

    def finish(self, body):
        """Authenticated caller requests shutdown of exactly the displayed session."""
        from hil_media import atomic
        with self.probe_lock:
            self.cached_at=-float('inf')
            state=self.state()
            key=body.get('session_key')
            if (not state.get('active') or not state.get('summary_fresh') or not state.get('can_finish')
                    or not isinstance(key,dict) or key!=state.get('session_key')):
                raise ValueError('会话已变化或不支持网页结束；请刷新，旧版在 F1 终端按 Ctrl+C')
            root=Path(state['receiver_runtime'])
            atomic(root/'finish-request.json',dict(schema='f1_finish_request_v1',**key,requested_unix=self.clock()))
            return dict(requested=True,run_id=key['run_id'],saved=False)

    def routes(self):
        """Read installed workflows; selecting a route never mutates configuration."""
        from hil_contract import read_session
        definitions=[('play','自由接入','直接遥操，不采集训练数据','teleop_play'),
                     ('demonstration','人工示教','30Hz 采集，留下可审核示教','demonstration'),
                     ('policy','策略 HIL','策略执行中接管与纠正','policy_hil')]
        try: installation=small_json(self.runtime/'hil-routes.json')
        except FileNotFoundError: installation={}
        try: common=self.commands(small_json(self.config_path))
        except (OSError,ValueError,KeyError): common=[]
        routes=[]
        for key,title,description,purpose in definitions:
            item=installation.get('routes',{}).get(key,{})
            configured=False;reason='等待后端安装';model=False
            try:
                launcher=Path(item['launcher']).expanduser().resolve(strict=True)
                session=read_session(Path(item['session_config']).expanduser())
                if session['purpose']!=purpose:raise ValueError('route/session purpose mismatch')
                configured=launcher.is_file();model=bool(session['policy']['endpoint'])
                reason='模型未绑定' if key=='policy' and not model else ''
                command='bash '+shlex.quote(str(launcher))+' '+key
            except (KeyError,OSError,ValueError,TypeError):command=None
            commands=[dict(x) for x in common]
            if commands:
                commands[0].update(command=command or '',hint=item.get('hint') or '现场执行后按 Enter，随后使用手柄 START。')
                if command and item.get('rtc_hil'):
                    commands[0]['command'] = ('bash '+shlex.quote(str(launcher))+' --continuous\n'
                        '# 现场按 Enter 后，用手柄 START 开始；需要零输出专项检查时添加 --shadow\n'
                        '# 定时：将 --continuous 换成 --duration-seconds 30')
            routes.append(dict(id=key,title=title,description=description,purpose=purpose,
                configured=configured,ready=configured and (key!='policy' or model),reason=reason,
                model_configured=model,candidate=bool(item.get('candidate',True)),commands=commands))
        return routes

    def next_config(self):
        from hil_contract import read_session, validate_session
        path=self.runtime/'hil-session.json'
        return read_session(path) if path.exists() else validate_session({})

    def save_config(self, value):
        from hil_contract import validate_session
        result=validate_session(value)
        with self.probe_lock:
            path=self.runtime/'hil-session.json';tmp=path.with_suffix('.tmp')
            tmp.write_text(json.dumps(result,ensure_ascii=False,allow_nan=False));tmp.replace(path)
            self.policy_check=None
        return dict(config=result,applies_to='next_session',robot_commands=False)

    def check_policy(self):
        from hil_contract import health
        if not self.probe_lock.acquire(blocking=False):raise ValueError('模型检查正在进行')
        try:
            config=self.next_config()
            if not config['policy']['endpoint']:raise ValueError('模型尚未配置')
            checked=health(config['policy'])
            self.policy_check=dict(checked_unix=self.clock(),health=checked)
            return dict(health=checked,robot_commands=False)
        finally:self.probe_lock.release()

    def probe(self, adapter, profile):
        """Atomic snapshot of existing subscribers, all receipts after request.
        No ROS initialization, output owner, acquisition queue or control lease.
        """
        import math
        from hil_contract import health, infer_request, validate_actions, NAMES, CONTRACT
        if not self.probe_lock.acquire(blocking=False):raise ValueError('模型检查正在进行')
        try:
            config=self.next_config();policy=config['policy'];identity=health(policy)
            store=getattr(adapter,'store',None)
            if store is None:raise ValueError('真实观测不可用')
            start=store.clock();deadline=time.monotonic()+2;observation=None
            while time.monotonic()<deadline:
                with store.lock:
                    now=store.clock()
                    cameras=[dict(store.cameras.get(k,{})) for k in ('head','left','right')]
                    joints=[dict(store.joints.get(k,{})) for k in NAMES[:14]]
                    grips=[dict(store.grippers.get(k,{})) for k in ('left','right')]
                    rows=cameras+joints+grips
                    fresh=store.connected and not store.error and all(start<x.get('received_at',0)<=now and now-x['received_at']<=.2 for x in rows)
                    if fresh and max(x['received_at'] for x in cameras)-min(x['received_at'] for x in cameras)<=.1:
                        observation=(now,cameras,joints+grips);break
                time.sleep(.01)
            if observation is None:raise ValueError('未收到请求之后的新三图与 state16')
            now,cameras,state=observation
            values=[x['position'] for x in state]
            if not all(type(x) in (int,float) and math.isfinite(x) for x in values):raise ValueError('无效 state16')
            metadata=dict(contract=CONTRACT,request_id='probe-'+os.urandom(8).hex(),observation_id=0,
                          policy_epoch=0,observation_monotonic_ns=round(min(x['received_at'] for x in cameras)*1e9),
                          state16=values,task=config['task'],chunk_steps=policy['chunk_steps'],frequency_hz=policy['frequency_hz'])
            result=infer_request(policy,metadata,[(name,x['data'],x['mime']) for name,x in zip(('head','left_wrist','right_wrist'),cameras)])
            actions=validate_actions(result,metadata,identity,profile['joint_limits_deg'])
            if store.clock()*1e9-metadata['observation_monotonic_ns']>2e9:raise ValueError('响应观测已过期')
            return dict(robot_commands=False,request_id=metadata['request_id'],model_id=identity['model_id'],
                        model_version=identity['model_version'],shape=[len(actions),16],
                        latency_ms=round((store.clock()-now)*1000,1),contract_valid=True,
                        semantic_acceptance=False)
        finally:self.probe_lock.release()

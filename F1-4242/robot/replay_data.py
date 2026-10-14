"""Read immutable F710 publication evidence. No ROS, output, or source writes."""
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path

ARMS = tuple(f'arm_{s}_j{i}' for s in ('l', 'r') for i in range(1, 8))
CONTEXT = tuple(f'head_j{i}' for i in range(1, 4)) + tuple(f'body_j{i}' for i in range(1, 6))
NAMES = ARMS + ('left_gripper_position', 'right_gripper_position')
KEYS = ('joint_command', 'left_gripper_command', 'right_gripper_command')
MAX_ROWS = 400000


def number(v, label):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(label + '必须为有限数值')
    return float(v)


def vector(v, n):
    if not isinstance(v, list) or len(v) != n:
        raise ValueError('录制向量维度不符')
    return tuple(number(x, '录制值') for x in v)


def integer(v):
    if type(v) is not int or v < 0:
        raise ValueError('录制时间或序号无效')
    return v


def file_bytes(root, name, maximum):
    p = Path(root) / name
    if any(x.is_symlink() for x in (p, *p.parents)) or not p.is_file():
        raise ValueError('录制文件缺失或存在软链接: ' + name)
    with p.open('rb') as f:
        data = f.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError('录制超过回放读取上限: ' + name)
    return data


@dataclass(frozen=True)
class Command:
    ns: int
    sequence: int
    values: tuple
    publications: tuple
    source: str


@dataclass(frozen=True)
class Recording:
    episode: str
    fingerprint: str
    robot: str
    profile_sha256: str
    commands: tuple
    observations: tuple
    events: tuple
    state_series: tuple = ()

    @property
    def origin(self):
        return self.commands[0].publications[0][1]

    @property
    def duration(self):
        return (self.commands[-1].ns - self.origin) / 1e9

    def select(self, start=0, end=None):
        start = number(start, '开始时间')
        end = self.duration if end is None else number(end, '结束时间')
        if not 0 <= start < end <= self.duration + 1e-9:
            raise ValueError('请选择动作时间轴内的连续区间')
        times = [c.publications[0][1] for c in self.commands]
        first = bisect_left(times, self.origin + round(start * 1e9))
        last = bisect_right(times, self.origin + round(end * 1e9))
        rows = self.commands[first:last]
        if len(rows) < 2:
            raise ValueError('所选区间至少需要两条真实命令')
        begin, finish = rows[0].publications[0][1], rows[-1].ns
        observations = [o for o in self.observations if begin - 250000000 <= o[0] <= finish + 250000000]
        if not observations or min(abs(o[0] - begin) for o in observations) > 250000000:
            raise ValueError('回放起点缺少相邻的身体/头部记录')
        context = min(observations, key=lambda o: abs(o[0] - begin))[1]
        events = [dict(time_s=(e['monotonic_ns']-self.origin)/1e9,
                       event=e['event'], reason=e.get('reason', ''),
                       selected=begin <= e['monotonic_ns'] <= finish) for e in self.events]
        return Segment(self, rows, tuple(observations), context, tuple(events))


@dataclass(frozen=True)
class Segment:
    recording: Recording
    commands: tuple
    observations: tuple
    context: tuple
    events: tuple

    @property
    def begin(self):
        return self.commands[0].publications[0][1]

    @property
    def duration(self):
        return (self.commands[-1].ns - self.begin) / 1e9

    def summary(self):
        r = self.recording
        return dict(episode_name=r.episode, fingerprint=r.fingerprint,
                    duration_s=self.duration, total_duration_s=r.duration,
                    start_s=(self.begin-r.origin)/1e9,
                    end_s=(self.commands[-1].ns-r.origin)/1e9,
                    command_count=len(self.commands), events=list(self.events),
                    initial_targets=dict(zip(ARMS, self.commands[0].values[:14])),
                    initial_grippers=dict(zip(('left', 'right'), self.commands[0].values[14:])),
                    context=dict(zip(CONTEXT, self.context)),
                    audit_scope='command_events_observations_sha256_not_image_payloads',
                    time_basis='first_command_publication', speed=1)


def read_recording(root):
    raw = file_bytes(root, 'journal/manifest.json', 2 * 1024**2)
    manifest = json.loads(raw)
    binding = manifest.get('binding', {})
    expected = dict(dimension=16, names=list(NAMES), units=['degree']*14+['0..100']*2)
    if (manifest.get('schema_version') != 3 or manifest.get('contract') != 'f1_upper_16d_v1'
            or manifest.get('action_contract') != expected or manifest.get('complete') is not True
            or manifest.get('status') != 'completed' or manifest.get('writer_error')
            or manifest.get('drop_count') != 0 or binding.get('purpose') != 'demonstration'
            or binding.get('execution_mode') != 'powered' or binding.get('synthetic')):
        raise ValueError('仅支持完整提交、真实 powered 的 F710 人工示教原件')
    episode = manifest.get('episode_id')
    streams = {}
    for name in ('control', 'observations', 'events'):
        content = file_bytes(root, 'journal/'+name+'.jsonl', 256 * 1024**2)
        if hashlib.sha256(content).hexdigest() != manifest.get('stream_sha256', {}).get(name):
            raise ValueError(name + ' 日志 SHA256 不一致')
        rows = []
        for line in content.splitlines():
            if len(line) > 1024**2 or len(rows) >= MAX_ROWS:
                raise ValueError('日志条目超过回放读取上限')
            row = json.loads(line)
            if row.get('episode_id') != episode or row.get('stream_sequence') != len(rows):
                raise ValueError('日志录制身份或流序号不一致')
            rows.append(row)
        if len(rows) != manifest.get('counts', {}).get(name):
            raise ValueError(name + ' 日志计数不一致')
        streams[name] = rows
    commands = []
    values, stamps = [None]*16, [None]*16
    previous, seq, last_publication = -1, -1, -1
    for row in streams['control']:
        at, current_seq = integer(row.get('monotonic_ns')), integer(row.get('command_sequence'))
        if at <= previous or current_seq <= seq:
            raise ValueError('控制时间或命令序号未严格递增')
        previous, seq = at, current_seq
        q = vector(row.get('submitted'), 16)
        if row.get('publication_complete') is not True or row.get('execution_mode') != 'powered':
            raise ValueError('命令缺少完整的真实发布证据；不使用实测值回填')
        pubs = row.get('publications')
        if not isinstance(pubs, list) or not pubs or pubs[0].get('topic_key') != KEYS[0]:
            raise ValueError('缺少手臂发布证据')
        seen, publications = set(), []
        for pub in pubs:
            key, ns = pub.get('topic_key'), integer(pub.get('monotonic_ns'))
            gid = pub.get('publisher_gid')
            if key not in KEYS or key in seen or not at-50000000 <= ns <= at or ns <= last_publication:
                raise ValueError('命令发布顺序、主题或时间无效')
            if not isinstance(gid, str) or len(gid) != 32 or not any(bytes.fromhex(gid)):
                raise ValueError('命令发布者身份无效')
            seen.add(key); last_publication = ns
            indices = range(14) if key == KEYS[0] else (14 if key == KEYS[1] else 15,)
            for i in indices:
                values[i], stamps[i] = q[i], ns
            publications.append((key, ns))
        recorded_stamps = row.get('action_source_timestamps_ns')
        if (not isinstance(recorded_stamps, list) or len(recorded_stamps) != 16
                or any(type(t) is not int for t in recorded_stamps)
                or stamps != recorded_stamps or tuple(values) != q):
            raise ValueError('动作值与逐字段发布/保持证据不一致')
        commands.append(Command(at, current_seq, q, tuple(publications), str(row.get('source', ''))))
    if len(commands) < 2:
        raise ValueError('没有足够的真实命令')
    observations = []
    state_series = [[] for _ in NAMES]
    for row in streams['observations']:
        at = integer(row.get('available_monotonic_ns'))
        full = vector(row.get('full22'), 22)
        if observations and at <= observations[-1][0]:
            raise ValueError('观测时间未严格递增')
        observations.append((at, full[14:]))
        # Comparison is optional for older journals, never invent feedback
        # from command targets or a camera timestamp.
        state, stamps = row.get('state16'), row.get('state_timestamps_ns')
        if state is not None and stamps is not None:
            state = vector(state, 16)
            if not isinstance(stamps, list) or len(stamps) != 16:
                raise ValueError('实测反馈时间戳维度不符')
            for i, (v, stamp) in enumerate(zip(state, stamps)):
                stamp = integer(stamp)
                if stamp > at or (state_series[i] and stamp < state_series[i][-1][0]):
                    raise ValueError('实测反馈时间戳无效')
                if state_series[i] and stamp == state_series[i][-1][0]:
                    if v != state_series[i][-1][1]:
                        raise ValueError('相同反馈时间戳对应不同实测值')
                else:
                    state_series[i].append((stamp, v))
    events = []
    for row in streams['events']:
        integer(row.get('monotonic_ns'))
        name = str(row.get('event', ''))
        if name.startswith('home_') or any(s in name for s in ('failed', 'fault', 'stopped', 'disconnect')):
            events.append(row)
    return Recording(episode, hashlib.sha256(raw).hexdigest(), binding.get('robot'),
                     binding.get('profile_sha256'), tuple(commands), tuple(observations), tuple(events),
                     tuple(tuple(s) for s in state_series))

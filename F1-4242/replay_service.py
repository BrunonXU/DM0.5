"""Episode-bound review tickets. HTTP clients never supply motion targets/paths."""
import hashlib
import json
from pathlib import Path
import secrets
import re
import threading
import time
import uuid

from robot.replay_data import read_recording
from robot.replay_action import validate, live_check, check_start, start_deviations


class ReplayService:
    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.tickets = {}

    def invalidate(self, sid):
        with self.lock:
            self.tickets = {k:v for k, v in self.tickets.items() if v['sid'] != sid}

    def idle(self):
        app = self.app
        if app.actions.busy():
            raise ValueError('请先结束当前工作台动作')
        if app.hil.state().get('active'):
            raise ValueError('请先在现场结束 HIL/示教控制会话')
        if app.teleop.state().get('phase') not in ('idle', 'failed', 'stopped', 'unavailable', 'not_configured'):
            raise ValueError('请先结束 Pico 遥操会话')
        if app.recording.state().get('mode') not in ('not_started', 'unavailable', 'idle', 'ready', 'review'):
            raise ValueError('请先结束当前数采')
        if app.data.recording().get('state') != 'no_process_observed':
            raise ValueError('录制占用状态尚未确认空闲')

    def load(self, episode_id, start, end):
        # The catalog owns path resolution; request bodies never supply paths.
        entry = self.app.data.episode(episode_id, refresh=True)
        root = Path(entry['path'])
        if not (root / 'journal/manifest.json').is_file():
            raise ValueError('此记录缺少可核验的原始发布日志与机器人绑定；仍可回看视频，不能以实测姿态代替命令复现')
        if entry.get('recording_status') != 'committed':
            raise ValueError('录制尚未完整提交；原件保留供排查')
        recording = read_recording(root)
        return recording.select(start, end), entry

    @staticmethod
    def alignment(segment):
        summary = segment.summary()
        return dict(kind='pose', targets={**summary['initial_targets'], **summary['context']},
                    grippers=summary['initial_grippers'])

    def report(self, report_id):
        if not re.fullmatch(r'[0-9a-f]{32}', report_id):
            raise ValueError('回放记录号无效')
        path = self.app.runtime/'replay_runs'/(report_id+'.json')
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64*1024**2:
            raise ValueError('回放记录尚未生成或不可读取')
        data = json.loads(path.read_text())
        return {k:v for k,v in data.items() if k != 'samples'}

    def recent(self, episode_id):
        from replay_reports import recent
        return recent(self.app.runtime/'replay_runs', episode_id)

    def review(self, sid, episode_id, start=0, end=None):
        self.invalidate(sid)
        segment, entry = self.load(episode_id, start, end)
        response = dict(segment.summary(), episode_id=episode_id, executable=False, can_align=False,
                        can_force_start=False, start_deviations=None,
                        issues=[], review_id=None, source_signature=entry['source_signature'])
        try:
            response['metrics'] = validate(self.app.adapter.profile, segment)
            self.idle()
        except (ValueError, RuntimeError, AttributeError, KeyError) as exc:
            response['issues'].append(str(exc) if not isinstance(exc, AttributeError) else '离线预览，未连接机器人')
            return response
        try:
            _, actual = live_check(self.app.adapter, segment)
            deviations = start_deviations(self.app.adapter.profile, segment, actual)
            response['start_deviations'] = deviations
            response['can_force_start'] = not any(row['outside_tolerance'] for row in deviations['grippers'])
            check_start(self.app.adapter.profile, segment, actual)
            response['executable'] = True
        except (ValueError, RuntimeError, AttributeError, KeyError) as exc:
            response['issues'].append(str(exc))
            # Same complete planner/guards as the existing pose action. This
            # only previews a path; GET never acquires control or moves.
            from robot.pose_review import review as pose_review
            try:
                alignment = pose_review(self.app.adapter, self.alignment(segment))
                response['can_align'] = alignment['executable']
                response['alignment_plan'] = alignment.get('plan')
                if not alignment['executable']:
                    response['issues'] += [x['message'] for x in alignment.get('issues', [])]
            except (ValueError, RuntimeError, AttributeError, KeyError) as error:
                response['issues'].append(str(error))
        if response['executable'] or response['can_align'] or response['can_force_start']:
            token = secrets.token_urlsafe(24)
            with self.lock:
                self.tickets = {k:v for k,v in self.tickets.items() if v['expires'] > time.monotonic() and v['sid'] != sid}
                while len(self.tickets) >= 32:
                    self.tickets.pop(next(iter(self.tickets)))
                self.tickets[token] = dict(sid=sid, expires=time.monotonic()+120, segment=segment,
                    episode_id=episode_id, start=start, end=end, signature=entry['source_signature'],
                    profile=self.profile_hash(), executable=response['executable'], can_align=response['can_align'],
                    can_force_start=response['can_force_start'])
            response.update(review_id=token, valid_for_s=120)
        return response

    def profile_hash(self):
        return hashlib.sha256(json.dumps(self.app.adapter.profile, sort_keys=True).encode()).hexdigest()

    def start(self, sid, body, *, align=False):
        if body.get('confirm') is not True:
            raise ValueError('请在现场明确确认开始实机回放')
        force_start = body.get('force_start', False)
        if type(force_start) is not bool:
            raise ValueError('强行回放标记必须为布尔值')
        if align and force_start:
            raise ValueError('回到录制起点不能使用强行回放标记')
        token = body.get('review_id')
        if not isinstance(token, str):
            raise ValueError('请先检查回放数据与起点')
        with self.lock:
            ticket = self.tickets.get(token)
            if not ticket or ticket['sid'] != sid or ticket['expires'] <= time.monotonic():
                raise ValueError('回放检查已失效，请重新检查')
        # Disk reads deliberately stay outside the operation mutex, so stop and
        # lease expiry remain responsive even with a large source journal.
        segment, entry = self.load(ticket['episode_id'], ticket['start'], ticket['end'])
        if segment.recording.fingerprint != ticket['segment'].recording.fingerprint or entry['source_signature'] != ticket['signature']:
            raise ValueError('录制来源已变化，请重新检查')
        eligibility = 'can_align' if align else 'can_force_start' if force_start else 'executable'
        if not ticket[eligibility]:
            raise ValueError('请重新检查起点并选择相应操作')
        if not align:
            # This scans the complete source. Keep stop/lease handling free;
            # the profile hash is checked again inside the operation mutex.
            validate(self.app.adapter.profile, segment)
        with self.app.op_lock:
            self.app.lease.require(sid, body.get('lease_id'), renew=True)
            self.idle()
            if self.profile_hash() != ticket['profile']:
                raise ValueError('机器人配置已变化，请重新检查')
            with self.lock:
                if self.tickets.get(token) is not ticket or ticket['expires'] <= time.monotonic():
                    raise ValueError('检查已使用或失效，不会重复启动')
                self.tickets.pop(token)
            if align:
                from robot.pose_review import review as pose_review
                pose = self.alignment(segment)
                checked = pose_review(self.app.adapter, pose)
                if not checked['executable']:
                    raise ValueError('；'.join(x['message'] for x in checked.get('issues', [])))
                if checked.get('plan', {}).get('head_boundary_recovery'):
                    if body.get('head_boundary_recovery_ack') is not True:
                        raise ValueError('需要明确确认原生头部边界恢复')
                    pose['head_boundary_recovery_ack'] = True
                pose['replay'] = dict(episode_id=ticket['episode_id'], phase='alignment')
                return self.app.actions.start(pose, sid)
            _, actual = live_check(self.app.adapter, segment, start=True, force_start=force_start)
            report_id = uuid.uuid4().hex
            metadata = dict(episode_id=ticket['episode_id'], episode_name=segment.recording.episode,
                            source_signature=ticket['signature'], created_at=time.time(),
                            fingerprint=segment.recording.fingerprint, start_s=segment.summary()['start_s'],
                            end_s=segment.summary()['end_s'], report_id=report_id,
                            force_start=force_start,
                            start_deviations=start_deviations(self.app.adapter.profile, segment, actual))
            spec = dict(kind='replay', _segment=segment, _force_start=force_start, replay=metadata,
                        _report_path=str(self.app.runtime/'replay_runs'/(report_id+'.json')))
            return self.app.actions.start(spec, sid)

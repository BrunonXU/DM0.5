"""One explicit, read-only Codex review at a time; publish only validated output."""
import copy
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time
import datetime as dt
from zoneinfo import ZoneInfo
import uuid

from hub import HERE, HubError, now


def codex_binary():
    configured = os.environ.get('WHEREWEARE_CODEX')
    if configured:
        return configured if Path(configured).is_file() and os.access(configured, os.X_OK) else None
    found = shutil.which('codex')
    if found:
        return found
    candidates = sorted((Path.home() / '.vscode-server/extensions').glob('openai.chatgpt-*/bin/linux-x86_64/codex'), reverse=True)
    return next((str(p) for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)


class ReviewJobs:
    def __init__(self, hub, store, binary=None):
        self.hub, self.store = hub, store
        self.binary = binary if binary is not None else codex_binary()
        self.path = hub.runtime / 'review-job.json'
        self.lock = threading.RLock()
        self.process = None
        self.auto_seen = None
        self.auto_stable_since = 0
        self.auto_attempted = None
        self.auto_checked = 0
        try:
            self.job = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.job = None
        if self.job and self.job.get('status') in {'queued', 'running'}:
            self.job.update(status='failed', error='服务已重启；上次审阅没有完成，请重新更新。')
            self._save()

    def _save(self):
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.job, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(self.path)

    def automation(self):
        with self.hub.connection() as conn:
            return self.hub.meta(conn, 'whereweare_auto', {'enabled': False, 'daily_limit': 8})

    def configure_automation(self, data):
        if set(data) - {'enabled', 'daily_limit'} or ('enabled' in data and type(data['enabled']) is not bool):
            raise HubError('无效自动更新设置')
        settings = {**self.automation(), **data}
        if type(settings['daily_limit']) is not int or not 1 <= settings['daily_limit'] <= 50:
            raise HubError('每日上限必须为1至50')
        with self.hub.mutation() as conn:
            self.hub.set_meta(conn, 'whereweare_auto', settings)
        self.auto_seen = None
        self.auto_attempted = None
        return settings

    def tick(self):
        # No scheduler is enabled by default. Opening a page never enables this.
        if time.monotonic() - self.auto_checked < 10:
            return
        self.auto_checked = time.monotonic()
        settings = self.automation()
        if not settings['enabled'] or not self.binary or (self.job and self.job['status'] in {'running', 'queued'}):
            return
        review = self.store.state()
        projects, _, manifest, _, _ = self.store._indexed(with_content=False)
        preferences = self.hub.tracking_preferences()
        current = {pid for pid, p in preferences.items() if p['activity'] == 'current' and not p['archived']}
        paths = [p for p in review['freshness']['changed_paths'] if current.intersection(manifest.get(p, {}).get('project_ids', []))]
        signature = json.dumps([(p, manifest.get(p, {}).get('sha256')) for p in paths])
        if not paths:
            self.auto_seen = None
            return
        if signature != self.auto_seen:
            self.auto_seen, self.auto_stable_since = signature, time.monotonic()
            return
        if signature == self.auto_attempted or time.monotonic() - self.auto_stable_since < 120:
            return
        date = dt.datetime.now(ZoneInfo(self.store.report_timezone)).date().isoformat()
        with self.hub.mutation() as conn:
            usage = self.hub.meta(conn, 'whereweare_auto_usage', {})
            count = usage.get('count', 0) if usage.get('date') == date else 0
            if count >= settings['daily_limit']:
                return
            self.hub.set_meta(conn, 'whereweare_auto_usage', {'date': date, 'count': count + 1})
        self.auto_attempted = signature
        self.start('update', project_ids=current)

    def state(self):
        with self.lock:
            return {'job': dict(self.job) if self.job else None,
                    'automation': self.automation(),
                    'runner': {'available': bool(self.binary),
                               'reason': '' if self.binary else '本机未找到 Codex；可在项目聊天中说 whereweare？完成更新。'}}

    def start(self, mode, date=None, project_ids=None):
        if not isinstance(mode, str) or mode not in {'update', 'daily', 'weekly'}:
            raise HubError('mode 必须为 update、daily 或 weekly')
        if mode in {'daily', 'weekly'}:
            period = self.store.report_period(mode, date)
            cached = self.store.cached_report(mode, date)
            if cached:
                return {'mode': mode, 'status': 'succeeded', 'cached': True, 'report': cached}
        else:
            period = {}
        if not self.binary:
            raise HubError('本机未找到 Codex；请在项目聊天中说 whereweare？', 503)
        with self.lock:
            if self.job and self.job['status'] in {'queued', 'running'}:
                raise HubError('已有一次审阅正在运行，请等待本次结果', 409)
            self.job = {'id': uuid.uuid4().hex, 'mode': mode, 'status': 'queued', 'started_at': now(), 'error': None, **period}
            self._save()
            thread = threading.Thread(target=self._run, args=(self.job['id'], mode, date, project_ids), daemon=True)
            thread.start()
            return dict(self.job)

    def _run(self, job_id, mode, date=None, project_ids=None):
        try:
            with self.lock:
                self.job['status'] = 'running'
                self._save()
            packet = self.store.prepare_report(mode, date) if mode in {'daily', 'weekly'} else self.store.prepare(mode, project_ids=project_ids)
            review_dir = self.hub.runtime / 'reviews' / packet['id']
            review_dir.mkdir(parents=True, exist_ok=True)
            output = review_dir / 'candidate.json'
            # Provider structured outputs require all object properties in required.
            # Local publication still permits its documented defaults.
            provider_schema = copy.deepcopy(self.store.schema)
            if packet.get('kind') == 'report':
                provider_schema = {'type': 'object', 'properties': {'report': provider_schema['properties']['report']}, 'required': ['report'], 'additionalProperties': False}
            def strict(value):
                if isinstance(value, dict):
                    if value.get('type') == 'object':
                        value['required'] = list(value.get('properties', {}))
                    for item in value.values():
                        strict(item)
                elif isinstance(value, list):
                    for item in value:
                        strict(item)
            strict(provider_schema)
            schema_path = review_dir / 'provider.schema.json'
            schema_path.write_text(json.dumps(provider_schema), encoding='utf-8')
            prompt = (HERE / 'prompts/review.md').read_text() + '\n\n' + (HERE / 'prompts' / (mode + '.md')).read_text()
            if packet.get('kind') == 'report':
                prompt = '你在生成已有项目记忆的回顾。只输出 report 对象；changes 仅引用 period_events。无变化写暂无新进展。不得更新项目和事件。日报一个明日建议，周报最多三个下周建议。'
            prompt += '\n\n只分析下面的 JSON 证据包，输出符合 schema 的 JSON。不要执行工具、命令或工作区操作。\n'
            prompt += json.dumps(packet, ensure_ascii=False)
            args = [self.binary, 'exec', '-c', 'model_reasoning_effort="high"',
                    '--sandbox', 'read-only', '--ephemeral', '--skip-git-repo-check',
                    '--cd', str(review_dir), '--output-schema', str(schema_path),
                    '--output-last-message', str(output), '--color', 'never', '-']
            with (review_dir / 'runner.log').open('w') as log:
                child = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=log, stderr=log,
                                         text=True, start_new_session=True)
                with self.lock:
                    self.process = child
                try:
                    child.communicate(prompt, timeout=600)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
                    raise HubError('审阅超时，旧摘要保留；可稍后重试。', 503)
            if child.returncode or not output.is_file():
                raise HubError('Codex 审阅未完成，旧摘要保留。请检查本机登录、额度或本次 runner.log。', 503)
            candidate = json.loads(output.read_text())
            published = self.store.publish_report(candidate, packet['id']) if packet.get('kind') == 'report' else self.store.publish(candidate, packet['id'])
            with self.lock:
                self.job.update(status='succeeded', finished_at=now(), revision=published.get('revision'), error=None)
                self._save()
        except Exception as error:
            with self.lock:
                message = str(error) if isinstance(error, HubError) else '审阅输出未通过检查，旧摘要保留；查看本次任务记录后重试。'
                self.job.update(status='failed', finished_at=now(), error=message)
                self._save()
        finally:
            with self.lock:
                self.process = None

    def close(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGTERM)

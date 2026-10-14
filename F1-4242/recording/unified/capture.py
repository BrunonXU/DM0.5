"""Bounded asynchronous JPEG capture. No media encoder or robot dependency."""
import copy
import hashlib
import json
import os
from pathlib import Path
import queue
import threading
import uuid
from .contract import CAMERAS, SCHEMA, atomic_json, confined, feature_contract, json_bytes, sha256, validate_row


class CaptureWriter:
    def __init__(self, root, metadata, *, queue_size=90):
        self.root = Path(root).absolute()
        confined(self.root, 'check')
        self.root.mkdir(parents=True, exist_ok=True)
        self.capture_id = str(metadata.get('capture_id') or uuid.uuid4().hex)
        if not self.capture_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in self.capture_id):
            raise ValueError('unsafe capture identity')
        self.path = self.root / (self.capture_id + '.partial')
        self.final = self.root / self.capture_id
        if self.final.exists():
            raise FileExistsError(self.final)
        self.path.mkdir(exist_ok=False)
        self.metadata = dict(metadata, **feature_contract(), capture_id=self.capture_id,
                             committed=False, training_ready=False)
        atomic_json(self.path/'capture.json', self.metadata)
        if type(queue_size) is not int or queue_size < 1: raise ValueError('bounded positive queue required')
        self.queue = queue.Queue(maxsize=queue_size)
        self.error = None
        self.submitted = self.written = 0
        self.closed = False
        self.thread = threading.Thread(target=self._run, name='F1CaptureWriter', daemon=True)
        self.thread.start()

    def append(self, row, images):
        if self.closed or self.error:
            raise RuntimeError('capture unavailable: ' + str(self.error or 'closed'))
        row = copy.deepcopy(row)
        validate_row(row)
        if set(images) != set(CAMERAS):
            raise ValueError('missing JPEG payload')
        try:
            self.queue.put_nowait((row, {k: bytes(images[k]) for k in CAMERAS}))
        except queue.Full as exc:
            self.error = 'capture queue overflow; source preserved, not committed'
            raise RuntimeError(self.error) from exc
        self.submitted += 1

    def append_extra(self, camera, frame, payload):
        if self.closed or self.error: raise RuntimeError('capture closed or failed')
        if camera not in CAMERAS: raise ValueError('invalid camera')
        try:
            self.queue.put_nowait((dict(_extra=True, camera=camera, **frame), {camera: bytes(payload)}))
        except queue.Full as exc:
            self.error = 'alignment queue overflow'
            raise RuntimeError(self.error) from exc

    def _run(self):
        packs = {}
        rows = extras = None
        try:
            packs = {k: (self.path/(k+'.pack')).open('xb') for k in CAMERAS}
            rows = (self.path/'samples.jsonl').open('xb')
            extras = (self.path/'extras.jsonl').open('xb')
            previous = None
            while True:
                item = self.queue.get()
                try:
                    if item is None:
                        break
                    row, images = item
                    if row.pop('_extra', False):
                        k = row['camera']; payload = images[k]; f = packs[k]
                        row.update(file=k+'.pack', offset=f.tell(), length=len(payload), sha256=hashlib.sha256(payload).hexdigest())
                        f.write(payload); extras.write(json_bytes(row)); continue
                    if previous is not None and row['time_ns'] <= previous:
                        raise ValueError('reference clock regressed')
                    previous = row['time_ns']
                    for k in CAMERAS:
                        payload = images[k]
                        if not payload.startswith(b'\xff\xd8'):
                            raise ValueError('expected original JPEG')
                        f = packs[k]
                        row['images'][k].update(file=k+'.pack', offset=f.tell(), length=len(payload),
                                               sha256=hashlib.sha256(payload).hexdigest())
                        f.write(payload)
                    row['row_index'] = self.written
                    rows.write(json_bytes(row))
                    self.written += 1
                finally:
                    self.queue.task_done()
            for f in [rows, extras, *packs.values()]:
                f.flush(); os.fsync(f.fileno())
        except Exception as exc:
            self.error = str(exc)
        finally:
            if rows: rows.close()
            if extras: extras.close()
            for f in packs.values(): f.close()

    def close(self, *, commit=True):
        if self.closed:
            return self.path
        self.closed = True
        if self.thread.is_alive():
            try: self.queue.put(None, timeout=5)
            except queue.Full: self.error = self.error or 'writer did not drain'
            self.thread.join(timeout=30)
        if self.thread.is_alive():
            self.error = self.error or 'writer join timeout'
        if self.error or self.written != self.submitted or not self.written:
            atomic_json(self.path/'failure.json', dict(error=self.error or 'empty/incomplete capture',
                         submitted=self.submitted, written=self.written, training_ready=False))
            raise RuntimeError(self.error or 'empty/incomplete capture')
        files = {p.name: sha256(p) for p in self.path.iterdir() if p.name.endswith(('.pack', '.jsonl'))}
        atomic_json(self.path/'capture.json', dict(self.metadata, complete=True, rows=self.written, files=files))
        if commit:
            self.path = commit_capture(self.path, self.final)
        return self.path


def commit_capture(path, destination):
    path, destination = Path(path), Path(destination)
    confined(path, 'capture.json'); confined(destination.parent, destination.name)
    if destination.exists(): raise FileExistsError(destination)
    if confined(path, 'failure.json').exists(): raise ValueError('failed capture cannot be committed')
    meta, rows = read_capture(path, allow_staged=True)
    if not rows: raise ValueError('empty capture')
    atomic_json(path/'capture.json', dict(meta, committed=True))
    os.rename(path, destination)
    return destination


def read_capture(root, *, verify=True, allow_staged=False):
    root = Path(root).absolute()
    meta = json.loads(confined(root, 'capture.json').read_text())
    if meta.get('schema') != SCHEMA or (not allow_staged and (meta.get('committed') is not True or root.name.endswith('.partial'))) or not meta.get('complete'):
        raise ValueError('capture is not completely committed')
    required = {'samples.jsonl', 'extras.jsonl', *(k+'.pack' for k in CAMERAS)}
    if set(meta.get('files', {})) != required: raise ValueError('missing capture integrity manifest')
    if verify:
        for name, digest in meta['files'].items():
            if sha256(confined(root, name)) != digest:
                raise ValueError('source hash mismatch: ' + name)
    rows = []
    with confined(root, 'samples.jsonl').open() as f:
        for line in f:
            rows.append(validate_row(json.loads(line)))
    if len(rows) != meta['rows']:
        raise ValueError('capture row count mismatch')
    return meta, rows


def read_image(root, frame):
    path = confined(root, frame['file'])
    offset, length = frame['offset'], frame['length']
    if type(offset) is not int or type(length) is not int or offset < 0 or not 0 < length <= 64*1024*1024:
        raise ValueError('invalid image byte range')
    with path.open('rb') as f:
        f.seek(offset); data = f.read(length)
    if len(data) != length or hashlib.sha256(data).hexdigest() != frame['sha256']:
        raise ValueError('image checksum mismatch')
    return data

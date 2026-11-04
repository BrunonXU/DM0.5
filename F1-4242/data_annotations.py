"""Portable recording annotations; only this named sidecar may be written in raw folders."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from core import atomic_json

NAME = 'f1-annotations.json'
SCHEMA = 'f1_recording_annotations_v1'


def reserved(name):
    return name == NAME or (name.startswith(NAME + '.') and name.endswith('.tmp'))


def path_for(recording):
    root = Path(recording['path'])
    if not root.is_absolute() or not root.is_dir() or any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError('录制目录不可用，无法保存随数据迁移的备注')
    path = root / NAME
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('录制备注文件路径无效')
    return path


@contextmanager
def lock(recording):
    # Directory locking adds no extra files to the recording and serializes
    # compare-and-swap across workbench processes sharing the same data root.
    path = path_for(recording)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def relocation_key(recording):
    # Transfer locator, not a training/content-integrity approval. A relocated
    # annotation restores notes only; historical approval is never transferred.
    files = sorted((f.get('relative_path', f.get('name')), f.get('size_bytes')) for f in recording.get('files', []) if not reserved(f.get('name', '')))
    return hashlib.sha256(json.dumps([recording.get('source'), recording.get('metadata_sha256'), files], sort_keys=True).encode()).hexdigest()


def read_document(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1_000_000:
            raise ValueError('备注文件大小或类型无效')
        document = json.load(stream)
    review = document.get('review') if isinstance(document, dict) else None
    if not isinstance(document, dict) or document.get('schema') != SCHEMA or not isinstance(review, dict) or not isinstance(review.get('notes', ''), str) or len(review.get('notes', '')) > 4000:
        raise ValueError('备注文件格式无效，保留原文件')
    return document


def load(recording):
    path = path_for(recording)
    if not path.exists():
        return None
    document = read_document(path)
    if document.get('review_present') is False:
        return None
    review = dict(document['review'])
    if document.get('recording_path') != str(path.parent) and document.get('relocation_key') == relocation_key(recording):
        review = dict(status='pending', notes=review.get('notes', ''), updated_at=None,
                      notes_updated_at=review.get('notes_updated_at'), notes_editor=review.get('notes_editor'),
                      episode_id=recording['id'], source_signature=recording['source_signature'],
                      metadata_sha256=recording.get('metadata_sha256'), source_relative_path=recording.get('relative_path'),
                      stale=False, relocated=True, previous_review=review)
    return review


def save(recording, review, *, create_only=False):
    path = path_for(recording)
    existing = {}
    if path.exists():
        existing = read_document(path)  # Never replace a corrupt/unrecognized file.
        if create_only:
            if existing['review'] != review:
                raise ValueError('已有随数据备注，禁止迁移覆盖')
            return path
    document = dict(existing, schema=SCHEMA, recording_path=str(path.parent), relocation_key=relocation_key(recording), review=review, review_present=True)
    if recording.get('capture_classification'):
        document['capture_classification'] = recording['capture_classification']
    atomic_json(path, document)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def save_capture_classification(recording):
    """Explicit migration only. GET/catalogue paths must never call this."""
    classification = recording.get('capture_classification')
    if not classification or classification.get('source_manifest_sha256') != recording.get('metadata_sha256'):
        raise ValueError('采集分类缺少原始来源绑定')
    with lock(recording):
        path = path_for(recording)
        if path.exists():
            document = read_document(path)
            if document.get('capture_classification') == classification:
                return path
            document['capture_classification'] = classification
            atomic_json(path, document)
        else:
            # Preserve absence of review, including the original "none" revision.
            review_present = recording['review'].get('revision', 'none') != 'none'
            review = {k: v for k, v in recording['review'].items() if k != 'revision'} if review_present else {}
            document = dict(schema=SCHEMA, recording_path=str(path.parent),
                relocation_key=relocation_key(recording), review=review,
                review_present=review_present, capture_classification=classification)
            atomic_json(path, document)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return path

"""Freeze an infra request into a new bound site; no process or motion side effects."""
import hashlib
import json
from pathlib import Path
import re

from .contracts import Task, digest
from .development import REVIEWED_FILES


def checked_request(body):
    if not isinstance(body, dict) or set(body) != {'task_id', 'config_revision', 'config'}:
        raise ValueError('完整任务请求字段不匹配。')
    if body['task_id'] != 'square_block':
        raise ValueError('当前只登记单方块到方孔任务。')
    config = body['config']
    if (not isinstance(config, dict) or set(config) != {'mode', 'instruction', 'corrections', 'note'}
            or config['mode'] not in ('astra','vla_astra') or type(config['corrections']) is not int
            or config['corrections'] not in (0, 1, 2, 3, 4)):
        raise ValueError('执行方式或纠偏预算无效。')
    for key, lo, hi in (('instruction', 1, 1000), ('note', 0, 240)):
        value = config[key]
        if (not isinstance(value, str) or not lo <= len(value.strip()) <= hi or value != value.strip()
                or any(ord(c) < 32 and c not in '\n\t' for c in value)):
            raise ValueError('任务说明或备注格式无效。')
    revision = hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if body['config_revision'] != revision:
        raise ValueError('任务配置身份不匹配，请刷新页面。')
    return json.loads(json.dumps(body))


def _isolate_run_sockets(documents,run_name):
    """Remap reviewed Harness IPC references without touching live filesystem state."""
    root='/home/user/Bruno/HARNESS/runtime/'+run_name
    definitions=(('owner.local.json','owner_socket','owner.sock'),
                 ('plan.local.json','socket','plan.sock'),
                 ('verify.local.json','socket','verify.sock'),
                 ('numerics.local.json','socket','numerics.sock'))
    replacements={}
    for name,key,filename in definitions:
        previous=documents[name].get(key)
        if (not isinstance(previous,str) or not previous.startswith('/')
                or '..' in previous.split('/') or previous in replacements):
            raise ValueError('invalid_reviewed_IPC_definition:'+name)
        replacement=root+'/'+filename
        if len(replacement.encode())>=108:raise ValueError('workspace_IPC_path_too_long')
        replacements[previous]=replacement
    socket_keys={'socket','owner_socket','planner_socket','verifier_socket','numerical_socket'}
    def remap(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key in socket_keys:
                    if not isinstance(item,str) or item not in replacements:
                        raise ValueError('unbound_reviewed_IPC_reference:'+key)
                    value[key]=replacements[item]
                else:remap(item)
        elif isinstance(value,list):
            for item in value:remap(item)
    for name in REVIEWED_FILES:remap(documents[name])
    return root


def freeze_site(template, destination, *, remote_site, request, run_name):
    from .evidence_pack import file_hash
    template, destination = Path(template), Path(destination)
    request = checked_request(request)
    if not re.fullmatch(r'web-[a-f0-9]{32}', run_name):
        raise ValueError('invalid_workspace_run_name')
    if not remote_site.startswith('/home/user/Bruno/HARNESS/site/web-') or '..' in remote_site.split('/'):
        raise ValueError('invalid_remote_workspace_site')
    record = json.loads((template / 'development.local.json').read_text())
    if record.get('scope', {}).get('goal') != 'placed':
        raise ValueError('workspace_requires_full_task_template')
    names = set(REVIEWED_FILES) | {'development.local.json', 'coarse-calibration.json',
                                'controller-reuse.json', 'parameter-sources.json'}
    documents = {}
    for name in names:
        path = template / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('regular_template_file_required:' + name)
        if name in REVIEWED_FILES and file_hash(path) != record['reviewed_files'][name]:
            raise ValueError('reviewed_template_changed:' + name)
        documents[name] = json.loads(path.read_text())
    ipc_directory=_isolate_run_sockets(documents,run_name)
    task = Task.model_validate(documents['task.local.json']).model_copy(update={
        'task_id': run_name, 'local_corrections': request['config']['corrections'],
        'operator_instruction': request['config']['instruction'].strip()})
    documents['task.local.json'] = task.model_dump(mode='json')
    for name in ('plan', 'verify'):
        documents[name + '.local.json']['task'] = task.model_dump(mode='json')
    for chain in (documents['plan.local.json']['fixture']['frame_chain'],
                  documents['gateway.local.json']['scene']['frame_chain']):
        chain['development_owner_config'] = remote_site + '/owner.local.json'
    documents['launch.local.json']['controller_config'] = remote_site + '/controller.local.json'
    if request['config']['mode']=='vla_astra':
        from .hybrid_site import configure_hybrid
        configure_hybrid(documents,template,remote_site=remote_site)
    record['scope']['local_corrections'] = task.local_corrections
    record['task_hash'] = digest(task)
    # A compatibility receipt belongs to its frozen run; acquire new evidence
    # for this task instead of carrying another run's boot authorization data.
    record.pop('boot_recheck', None)
    record['user_acceptance'].update(
        statement='通过infra点击开始完整单块流程，不使用手柄或Enter；停止请求由owner保持并核对反馈。',
        evidence_id='user-conversation-20261105-infra-without-gamepad')
    record['workspace_request'] = dict(run_name=run_name, config_revision=request['config_revision'],
                                     ipc_directory=ipc_directory)
    destination.mkdir(parents=True, exist_ok=False)
    def write(name, value):
        (destination / name).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
    for name, value in documents.items():
        if name != 'development.local.json':
            if name in ('coarse-calibration.json', 'controller-reuse.json'):
                # Referenced bytes are immutable; do not reserialize their identity.
                (destination / name).write_bytes((template / name).read_bytes())
            else:
                write(name, value)
    record['reviewed_files'] = {name: file_hash(destination / name) for name in REVIEWED_FILES}
    write('development.local.json', record)
    write('workspace-config.json', request)
    return task

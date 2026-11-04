"""Package the F1 RLT gamepad sender with its complete frozen import closure."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def build(output):
    root=Path(__file__).resolve().parents[1]/'windows_client'
    names=['scripts/03_windows_f1_sender.py','scripts/03_windows_shadow_sender.py']
    names += ['src/dagger_core/'+name+'.py' for name in (
        '__init__','protocol','async_transport','input_recovery',
        'f710_mapping','gamepad_windows','sender_guidance')]
    payload={name:(root/name).read_bytes() for name in names}
    manifest={name:hashlib.sha256(data).hexdigest() for name,data in payload.items()}
    output.mkdir(parents=True,exist_ok=True)
    archive=output/'windows-rlt-local-20261027-r2.zip'
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
        for name,data in payload.items():z.writestr(name,data)
        z.writestr('manifest.json',json.dumps(manifest,indent=2))
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    template=(root/'update_input_pause.ps1').read_text()
    template=template.replace('20261015-input-pause-v31-r2/windows-input-pause-v31-r2.zip',
        '20261027-rlt-local-r2/windows-rlt-local-20261027-r2.zip')
    template=template.replace('input-pause-r2-','rlt-local-20261027-')
    template=template.replace('Windows sender v31-r2','Windows sender RLT 局部任务 v2')
    template=template.replace('沿用 scripts/windows_f1_sender.cmd。输入短暂停顿：松 RB、摇杆回中，等提示后再按 RB；START 结束本条。',
        '沿用 scripts/windows_f1_sender.cmd。默认右臂；RLT模式START开条/人工接管/交回VLA，十字键右每条进入一次actor，十字键上确认局部成功，Y复位并稳定两秒后保存。其他模式沿用原START行为。以接收端模式回执为准。')
    template=template.replace('__ZIP_SHA256__',digest)
    (output/'update_rlt_gamepad.ps1').write_text(template,encoding='utf-8-sig')
    (output/'package.json').write_text(json.dumps(dict(sha256=digest,files=manifest),indent=2)+'\n')
    print(json.dumps(dict(archive=str(archive),sha256=digest,files=len(manifest))))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    build(parser.parse_args().output)

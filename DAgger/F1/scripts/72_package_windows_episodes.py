"""Package the F1 continuous-episode sender with its complete frozen import closure."""
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
    archive=output/'windows-episodes-20261021.zip'
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
        for name,data in payload.items():z.writestr(name,data)
        z.writestr('manifest.json',json.dumps(manifest,indent=2))
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    template=(root/'update_input_pause.ps1').read_text()
    template=template.replace('20261015-input-pause-v31-r2/windows-input-pause-v31-r2.zip',
        '20261021-continuous-episodes/windows-episodes-20261021.zip')
    template=template.replace('input-pause-r2-','episodes-20261021-')
    template=template.replace('Windows sender v31-r2','Windows sender 连续分条 20261021')
    template=template.replace('沿用 scripts/windows_f1_sender.cmd。输入短暂停顿：松 RB、摇杆回中，等提示后再按 RB；START 结束本条。',
        '沿用 scripts/windows_f1_sender.cmd。默认右臂；示教/RTC-HIL统一START开始，策略中START接管，人工阶段START不交还；松RB、Y回位停稳2秒后按十字键上保存，保存完成再START下一条。需配套新F1接收端。')
    template=template.replace('__ZIP_SHA256__',digest)
    (output/'update_episodes.ps1').write_text(template,encoding='utf-8-sig')
    (output/'package.json').write_text(json.dumps(dict(sha256=digest,files=manifest),indent=2)+'\n')
    print(json.dumps(dict(archive=str(archive),sha256=digest,files=len(manifest))))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    build(parser.parse_args().output)

"""Audited offline original-image review; no server, training approval or ROS.

Run ``python -m f1dagger.review RUN --output NEW_DIR`` and open index.html.
Images are extracted byte-for-byte from either archive format. Playback follows
recorded host receipt times, and never pads/repeats images to claim a frame rate.
"""
import argparse
import bisect
import json
from pathlib import Path
import shutil

from .contract import CAMERAS, FEATURE_NAMES
from .payload_archive import read_payload
from .recording import audit, read_rows


HTML = r'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>F1 原始数据审阅</title>
<style>
body{font:15px system-ui,sans-serif;margin:24px;background:#10151d;color:#edf2fa}h1{font-size:23px}button,select{font:inherit;padding:7px 12px;margin-right:8px}a{color:#90c8ff}.notice{padding:12px;background:#47351c;border-left:4px solid #ffc36a;white-space:pre-wrap}.row{display:flex;gap:14px;flex-wrap:wrap;margin:16px 0}.view{flex:1;min-width:220px;margin:0}.view img{display:block;width:100%;aspect-ratio:4/3;object-fit:contain;background:#05080b}.view figcaption{font-size:13px;white-space:pre-wrap;margin:6px 0}#seek{width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#1a2330;padding:12px}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:5px 8px;border-bottom:1px solid #344151;text-align:left}.muted{color:#bbc5d5}
</style>
<h1>F1 原始数据审阅</h1><p id="summary"></p><p id="notice" class="notice"></p>
<p class="muted">三图按同一 observation 展示；主机接收对齐不等于曝光同步。播放使用实际接收间隔，不补帧、不假装 30 fps。此页不会批准纠正片段。</p>
<div class="row"><button id="prev">上一帧</button><button id="play">播放</button><button id="next">下一帧</button><label>播放速度 <select id="speed"><option value="0.25">0.25×</option><option value="0.5">0.5×</option><option selected value="1">1×</option><option value="2">2×</option></select></label></div>
<input id="seek" type="range" min="0" value="0" aria-label="观测帧"><p id="position"></p>
<div class="row" id="images"></div><pre id="context"></pre>
<p class="muted">action16 仅显示所选控制记录的实际 submitted；没有发布则为 None。不会用 selected、候选或反馈填补动作。控制记录按 observation 可用时刻向前选择，年龄及跨模式情况均显示。</p>
<table><thead><tr><th>字段（臂：度；夹爪：0–100）</th><th>state16 反馈</th><th>action16 实际 submitted</th></tr></thead><tbody id="vectors"></tbody></table>
<p><a href="review_data.json">完整审阅索引</a> · <a href="audit.json">原始审计</a> · <a href="control.jsonl">全部原始控制记录</a> · <a href="events.jsonl">全部原始事件</a></p>
<script id="review-data" type="application/json">__DATA__</script>
<script>
'use strict';
const data=JSON.parse(document.getElementById('review-data').textContent), rows=data.observations;
const $=id=>document.getElementById(id);let index=0,playing=false,baseClock=0,baseElapsed=0,requestId=0;
$('summary').textContent=`${data.episode_id} · ${data.task} · ${rows.length} 组观测，${data.image_count} 张原始图 · ${data.execution_mode}`;
$('notice').textContent=data.notices.join('\n');$('seek').max=Math.max(0,rows.length-1);
const views=data.cameras.map(camera=>{const figure=document.createElement('figure');figure.className='view';const img=document.createElement('img');img.alt=camera;const caption=document.createElement('figcaption');figure.append(img,caption);$('images').append(figure);return {img,caption};});
const cells=data.feature_names.map(name=>{const tr=document.createElement('tr');const label=document.createElement('td');label.textContent=name;const state=document.createElement('td'),action=document.createElement('td');tr.append(label,state,action);$('vectors').append(tr);return {state,action};});
const fmt=x=>x===null||x===undefined?'None':Number(x).toFixed(4);
function stop(){playing=false;$('play').textContent='播放';}
async function show(i){
 index=Math.max(0,Math.min(rows.length-1,i));const row=rows[index],token=++requestId;if(!row)return;
 $('seek').value=index;const loaded=await Promise.all(row.frames.map(f=>new Promise(resolve=>{if(!f.extracted_path){resolve(null);return;}const img=new Image();img.onload=()=>resolve(img);img.onerror=()=>resolve(null);img.src=f.extracted_path;})));
 if(token!==requestId)return;
 views.forEach((view,j)=>{const f=row.frames[j];if(loaded[j]){view.img.src=loaded[j].src;view.img.hidden=false;}else{view.img.removeAttribute('src');view.img.hidden=true;}view.caption.textContent=`${f.camera} · frame ${f.frame_id}\n接收 +${f.received_elapsed_ms.toFixed(3)} ms · 原时钟 ${f.timestamp.value} ${f.timestamp.unit}\n${f.extracted_path?'':'此观测未保存图片'}`;});
 $('position').textContent=`${index+1}/${rows.length} · observation ${row.observation_id} · 接收 +${(row.elapsed_ms/1000).toFixed(3)} s · 下一间隔 ${index+1<rows.length?(rows[index+1].elapsed_ms-row.elapsed_ms).toFixed(3)+' ms':'结束'}`;
 $('context').textContent=JSON.stringify({observation_id:row.observation_id,host_received_monotonic_ns:row.reference_monotonic_ns,observation:{state:row.hil_state,intervention:row.intervention_id,epoch:row.policy_epoch},control:row.control,action16:row.action16===null?'None':row.action16},null,2);
 cells.forEach((cell,j)=>{cell.state.textContent=fmt(row.state16[j]);cell.action.textContent=row.action16===null?'None':fmt(row.action16[j]);});
}
function tick(now){if(!playing)return;const elapsed=baseElapsed+(now-baseClock)*Number($('speed').value);let lo=0,hi=rows.length;while(lo<hi){const mid=(lo+hi)>>1;if(rows[mid].elapsed_ms<=elapsed)lo=mid+1;else hi=mid;}const next=Math.max(0,lo-1);if(next!==index)show(next);if(next>=rows.length-1){stop();return;}requestAnimationFrame(tick);}
$('play').onclick=()=>{if(playing){stop();return;}if(!rows.length)return;if(index===rows.length-1)show(0);playing=true;$('play').textContent='暂停';baseElapsed=rows[index].elapsed_ms;baseClock=performance.now();requestAnimationFrame(tick);};
$('seek').oninput=()=>{stop();show(Number($('seek').value));};$('prev').onclick=()=>{stop();show(index-1);};$('next').onclick=()=>{stop();show(index+1);};$('speed').onchange=()=>{if(playing){baseElapsed=rows[index].elapsed_ms;baseClock=performance.now();}};
document.addEventListener('keydown',e=>{if(e.target.tagName==='INPUT'||e.target.tagName==='SELECT')return;if(e.key==='ArrowLeft')$('prev').click();if(e.key==='ArrowRight')$('next').click();if(e.code==='Space'){e.preventDefault();$('play').click();}});
if(rows.length)show(0);else{$('position').textContent='此完整记录没有观测帧';for(const id of ['prev','play','next','seek'])$(id).disabled=true;}
</script></html>
'''


def build_review(run, output):
    run, output = Path(run).resolve(), Path(output).resolve()
    partial = output.with_name(output.name + '.partial')
    if output == run or run in output.parents or partial == run or run in partial.parents:
        raise ValueError('review output must be outside the immutable raw episode')
    if output.exists() or partial.exists():
        raise FileExistsError('refusing to overwrite existing review or partial directory')
    report = audit(run)
    if not report['valid']:
        raise ValueError('raw audit failed: ' + '; '.join(report['errors']))
    manifest = json.loads((run/'journal/manifest.json').read_text())
    observations = read_rows(run/'journal/observations.jsonl')
    payload = {r['observation_id']: r for r in read_rows(run/'payload/frames.jsonl')}
    controls = read_rows(run/'journal/control.jsonl')
    control_times = [r['monotonic_ns'] for r in controls]
    origin = max(f['host_received_monotonic_ns'] for f in observations[0]['frames']) if observations else 0
    data = dict(schema='f1_offline_review_v1', episode_id=manifest['episode_id'],
        task=manifest['task_prompt'], raw_run=str(run), source_manifest_sha256=report['manifest_sha256'],
        execution_mode=manifest['binding'].get('execution_mode', 'unknown'),
        synthetic=manifest['binding'].get('synthetic') is True, training_ready=False,
        accepted_interventions=[], cameras=CAMERAS, feature_names=FEATURE_NAMES,
        timeline='latest_camera_host_receipt_no_resampling', image_count=0, observations=[],
        notices=['仅供审阅：未批准任何 intervention；训练导出需要独立人工审阅。'])
    if data['execution_mode'] != 'powered':
        data['notices'].append('SHADOW / 非实机输出记录：不可作为专家机器人动作数据。')
    if data['synthetic']:
        data['notices'].append('合成输入记录：不可当作现场人工纠正验收。')
    if report['observations_without_payload']:
        data['notices'].append(f"{report['observations_without_payload']} 组观测未保存图片，将明确显示缺图，不用缓存填补。")
    partial.mkdir(parents=True)
    for ordinal, obs in enumerate(observations):
        received = max(f['host_received_monotonic_ns'] for f in obs['frames'])
        control_index = bisect.bisect_right(control_times, obs['available_monotonic_ns'])-1
        control = controls[control_index] if control_index >= 0 else None
        row = dict(observation_id=obs['observation_id'], elapsed_ms=(received-origin)/1e6,
            reference_monotonic_ns=str(received), available_monotonic_ns=str(obs['available_monotonic_ns']),
            hil_state=obs['hil_state'], intervention_id=obs['intervention_id'], policy_epoch=obs['policy_epoch'],
            state16=obs['state16'], state_timestamps_ns=[str(t) for t in obs['state_timestamps_ns']],
            action16=None if control is None else control['submitted'], frames=[], control=None)
        if control is not None:
            row['control'] = {k: control.get(k) for k in ('stream_sequence','command_sequence','source',
                'hil_state','intervention_id','policy_epoch','publication_complete','execution_mode')}
            row['control'].update(monotonic_ns=str(control['monotonic_ns']),
                age_at_observation_available_ms=(obs['available_monotonic_ns']-control['monotonic_ns'])/1e6,
                same_context=all(control[k] == obs[k] for k in ('hil_state','intervention_id','policy_epoch')),
                action_source_timestamps_ns=[str(t) for t in control.get('action_source_timestamps_ns', [])])
        archived = payload.get(obs['observation_id'])
        for camera_index, original in enumerate(obs['frames']):
            frame = dict(original, host_received_monotonic_ns=str(original['host_received_monotonic_ns']),
                timestamp=dict(original['timestamp'], value=str(original['timestamp']['value'])),
                received_elapsed_ms=(original['host_received_monotonic_ns']-origin)/1e6,
                extracted_path=None)
            if archived is not None:
                stored = archived['frames'][camera_index]
                suffix = {'image/jpeg': 'jpg', 'image/png': 'png'}.get(stored['media_type'])
                if suffix is None:
                    raise ValueError('unsupported original image media type')
                relative = f'images/{original["camera"]}/{ordinal:09d}.{suffix}'
                destination = partial/relative; destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(read_payload(run/'payload', stored))
                frame.update(extracted_path=relative, payload_sha256=stored['payload_sha256'],
                             payload_bytes=stored['payload_bytes'])
                data['image_count'] += 1
            row['frames'].append(frame)
        data['observations'].append(row)
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    (partial/'review_data.json').write_text(encoded + '\n', encoding='utf-8')
    # Prevent recorded text from closing the inert JSON script element.
    inline = encoded.replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    (partial/'index.html').write_text(HTML.replace('__DATA__', inline), encoding='utf-8')
    (partial/'audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for name in ('control.jsonl', 'events.jsonl'):
        shutil.copyfile(run/'journal'/name, partial/name)
    partial.rename(output)
    return dict(output=str(output), index=str(output/'index.html'), observations=len(observations),
                images=data['image_count'], training_ready=False, accepted_interventions=[])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build_review(args.run, args.output)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, str(exc) + '\n')
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Rebuild concise latency tables from retained offline evidence."""
import csv
import hashlib
import json
from pathlib import Path

TASK = Path(__file__).resolve().parents[1]
ROOT = TASK / 'artifacts/20261023-rlinf-offline'


def read(path):
    return json.loads((ROOT / path).read_text())


LIVE_FIELDS = {
    'observation_capture_to_request_ms': '采集→发请求（观测年龄）',
    'pack_ms': '客户端图像处理与封包',
    'server_decode_ms': '服务端解码',
    'server_preprocess_ms': '模型输入预处理',
    'server_model_h2d_d2h_ms': '模型计时字段model_h2d_d2h',
    'server_ik_ms': '受限连续IK',
    'server_worker_queue_ms': '服务端worker排队',
    'server_handler_ms': '服务handler总计（包含上述子项）',
    'network_rtt_ms': 'RPC往返（包含服务时间）',
    'rtt_minus_handler_ms': 'RTT减handler（网络与调度剩余）',
    'client_total_ms': '客户端请求总计',
    'observation_to_response_ms': '采集→返回',
    'response_to_adoption_ms': '返回→采纳（已采纳请求）',
}


def live_table(metrics):
    lines = ['| 部分 | P50 ms | P95 ms | P99 ms | max ms |',
             '|---|---:|---:|---:|---:|']
    for key, label in LIVE_FIELDS.items():
        if key in metrics:
            lines.append('| ' + label + ' | ' + ' | '.join(f'{metrics[key][q]:.2f}' for q in ('p50','p95','p99','max')) + ' |')
    return '\n'.join(lines)


def model_summary():
    specs = [
        ('inactive-cache-parent-r2', '无效左图视觉缓存：父1K', 'YES，集中验收'),
        ('inactive-cache-A-r2', '同候选：A1250', 'YES，集中验收'),
        ('inactive-cache-B-r2', '同候选：B1250', 'YES，集中验收'),
        ('jacobian_identity', '近似雅可比I', 'NO，新增拒绝'),
        ('upstream_approx', '完整上游approx', 'NO，拒绝/释放改变'),
        ('prefix_compile-r2', '精度约束prefix编译', 'NO，输出/释放改变'),
        ('prefix_constants-r2', 'prefix常量构造调整', '不单独采用，收益很小'),
        ('prefix_graph-r2', 'eager prefix CUDA Graph', '待数据判定'),
        ('prefix-graph-parent-r4', 'prefix Graph关闭AMP权重缓存：父1K', '待数据判定'),
        ('prefix-graph-A-r4', '同候选：A1250', '待数据判定'),
        ('prefix-graph-B-r4', '同候选：B1250', '待数据判定'),
    ]
    lines = ['| 候选 | policy P50基线→候选 ms | P95基线→候选 ms | raw完全一致 | 新增IK拒绝 | 判定 |',
             '|---|---:|---:|---:|---:|---|']
    rows, decisions = [], {}
    for name, label, decision in specs:
        folder = ROOT / 'model' / name
        if not (folder / 'summary.json').exists():
            trace = folder / 'prefix-failure.txt'
            if not trace.exists():
                trace = ROOT / 'model' / (name+'.log')
            assert trace.exists(), f'Incomplete evidence: {name}'
            lines.append(f'| {label} | 未完成 | 未完成 | 仅54/54 guided通过 | 未评估完整集 | NO，bootstrap转换时CUDA异常 |')
            decisions[name] = {'decision':'NO','reason':'guided-to-bootstrap transition CUDA failure','trace':str(trace.relative_to(ROOT))}
            continue
        s = json.loads((folder / 'summary.json').read_text())
        latency = s['latency_both_success']
        b, c = latency['baseline']['policy'], latency['candidate']['policy']
        if name == 'prefix_graph-r2' or name.startswith('prefix-graph-'):
            decision = 'YES，集中验收' if s['exact'] == s['comparison_count'] and not s['new_failures'] else 'NO，数值或行为改变'
        if name.startswith('inactive-cache'):
            assert s['exact'] == s['comparison_count'] and s['new_failures'] == 0
        lines.append(f'| {label} | {b["p50"]:.2f} → {c["p50"]:.2f} | {b["p95"]:.2f} → {c["p95"]:.2f} | {s["exact"]}/{s["comparison_count"]} | {s["new_failures"]} | {decision} |')
        timings=[json.loads(line) for line in (folder/'timings.jsonl').read_text().splitlines()]
        pairs={}
        for row in timings:
            pairs.setdefault(row['pair'],{})[row['candidate']]=row
        deltas=sorted(p[False]['policy']-p[True]['policy'] for p in pairs.values() if p[False]['success'] and p[True]['success'])
        n=len(deltas)
        median=(deltas[(n-1)//2]+deltas[n//2])/2
        decisions[name]={'decision':decision,'comparisons':s['comparison_count'],'exact':s['exact'],
                         'baseline_failures':s['baseline_failures'],'candidate_failures':s['candidate_failures'],
                         'new_failures':s['new_failures'],'origins':s['origins'],
                         'both_success_timing_pairs':s['both_success_timing_pairs'],
                         'median_paired_policy_saving_ms':median,
                         'executable_open_changed':s['executable_open_changed']}
        for side in ('baseline','candidate'):
            for component, values in latency[side].items():
                rows.append(dict(experiment=name,side=side,component=component,n=s['both_success_timing_pairs'],**values))
    with (ROOT/'model-latency.csv').open('w') as f:
        w=csv.DictWriter(f,fieldnames=['experiment','side','component','n','p50','p95','p99','max']);w.writeheader();w.writerows(rows)
    (ROOT/'model-decision.json').write_text(json.dumps(decisions,ensure_ascii=False,indent=2)+'\n')
    return '\n'.join(lines)


def replace_section(text, name, content):
    start, end = f'<!-- {name}_START -->', f'<!-- {name}_END -->'
    if start in text:
        a, remainder = text.split(start,1)
        _, b = remainder.split(end,1)
        return a+start+'\n'+content+'\n'+end+b
    return text.replace(f'<!-- {name} -->',start+'\n'+content+'\n'+end)


if __name__ == '__main__':
    report=TASK/'inference/experiments/20261023-rlinf-offline.md'
    text=report.read_text()
    ab=read('network/historical-ab-summary.json')
    text=replace_section(text,'LIVE_TABLE',live_table(ab['groups']['continuation_success']['metrics']))
    full=read('network/historical-full-restored-summary.json')
    full_section='### 恢复旧Full1K：本日另列\n\n本日3条已结束记录共447请求，全部RPC成功、445采纳、零RTC截止暂停；两次未采纳位于结束边界。下面是444次稳态续接；场景与A/B不同，不能据此排名模型。\n\n'+live_table(full['groups']['continuation_success']['metrics'])
    text=replace_section(text,'FULL_TABLE',full_section)
    text=replace_section(text,'MODEL_TABLE',model_summary())
    report.write_text(text)
    source={str(p.relative_to(TASK)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (TASK/'scripts').glob('*rlinf*.py')}
    source.update({str(p.relative_to(TASK)):hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in (TASK/'scripts').glob('*rlinf*.sh')})
    (ROOT/'source-sha256.json').write_text(json.dumps(source,indent=2)+'\n')
    print(json.dumps({'report':str(report),'sources':len(source)}))

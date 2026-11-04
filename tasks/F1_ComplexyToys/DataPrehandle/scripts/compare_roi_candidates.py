#!/usr/bin/env python3
"""Compare real ROI candidates using the existing model-image processor, on CPU."""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

TASK = Path(__file__).resolve().parents[2]
PROJECTS = TASK.parents[1]
sys.path.insert(0, str(TASK / 'src'))
from f1_toys.processing import crop_box, load_contract, process_image, sha256

CANDIDATES = [
    ('A', '当前 ROI', [.24, .04, .96, 1.]),
    ('B', '仅收紧顶部', [.24, .12, .96, 1.]),
    ('C', '顶部 + 右侧', [.24, .12, .92, 1.]),
]


def font(size, bold=False):
    name = 'NotoSansCJK-Bold.ttc' if bold else 'NotoSansCJK-Regular.ttc'
    return ImageFont.truetype('/usr/share/fonts/opentype/noto/' + name, size)


def label(draw, xy, words, size=22, color='#233449', bold=False):
    draw.text(xy, words, font=font(size, bold), fill=color, spacing=8)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=TASK / 'DataPrehandle/visualizations/20261016-roi-comparison')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    out = args.output
    cfg_path = TASK / 'configs/roi_right_v1.json'
    cfg = load_contract(cfg_path)
    protected = [cfg_path, TASK / 'src/f1_toys/processing.py']
    before = {str(p): sha256(p) for p in protected}
    v1 = TASK / 'artifacts/20261015-v1-smoke/raw/f1_hil_20261014T091553Z_88a4ad17'
    history = PROJECTS / 'DAgger/artifacts/results/20261013-resampling-fix/raw-copies/f1_hil_20261013T073200Z_17800fe8'
    vrows = [json.loads(s) for s in (v1 / 'payload/frames.jsonl').read_text().splitlines()]
    hrows = [json.loads(s) for s in (history / 'payload/frames.jsonl').read_text().splitlines()]
    samples_path = TASK / 'artifacts/20261015-v1-smoke/dataset/samples.jsonl'
    samples = [json.loads(s) for s in samples_path.read_text().splitlines()]
    by_obs = {r['observation_id']: i for i, r in enumerate(vrows)}
    key_rows = [by_obs[samples[i]['observation_id']] for i in (150, 450, 660)]
    key_rows.insert(2, 510)  # Previously observed rightward excursion.
    variants = []
    for ident, name, normalized in CANDIDATES:
        c = copy.deepcopy(cfg)
        c['head_roi_xyxy_normalized'] = normalized
        box = crop_box((1280, 720), c)
        w, h = box[2] - box[0], box[3] - box[1]
        ratio = min(224 / w, 224 / h)
        rw, rh = round(w * ratio), round(h * ratio)
        variants.append(dict(id=ident, name=name, config=c, box=box,
                             content_size=[rw, rh], linear_scale_vs_A=922 / w,
                             padding_top_bottom=[(224-rh)//2, 224-rh-(224-rh)//2]))
    manifest = dict(date='2026-10-16', timezone='Asia/Shanghai',
                    status='ROI comparison only; production configuration unchanged',
                    candidates=variants, sources=[], files={},
                    processor_hashes_before=before, script_sha256=sha256(Path(__file__)),
                    augmentation='disabled for all candidates to isolate fixed ROI geometry',
                    observation_limit='visual samples, not exhaustive detection or training-quality evaluation')
    manifest['source_indexes'] = {str(p): sha256(p) for p in [v1/'payload/frames.jsonl', history/'payload/frames.jsonl', samples_path]}
    cache = {}

    def get_record(root, rows, index, prefix):
        key = (prefix, index)
        if key in cache:
            return cache[key]
        row = rows[index]
        f = next(f for f in row['frames'] if f['camera'] == 'head')
        with (root / 'payload' / f['path']).open('rb') as stream:
            stream.seek(f['payload_offset'])
            data = stream.read(f['payload_bytes'])
        assert hashlib.sha256(data).hexdigest() == f['payload_sha256']
        with Image.open(io.BytesIO(data)) as im:
            raw = im.convert('RGB')
        folder = out / 'pixels' / f'{prefix}-raw-{index:04d}'
        folder.mkdir(parents=True)
        raw.save(folder / 'original.png')
        views = []
        for var in variants:
            cropped = raw.crop(var['box'])
            pixels = process_image(raw, 'head', var['config'])
            assert pixels.shape == (224, 224, 3) and pixels.dtype == np.uint8
            current = Image.fromarray(pixels)
            cropped.save(folder / f'{var["id"]}-cropped.png')
            current.save(folder / f'{var["id"]}-model-224.png')
            views.append((cropped, current))
        # A is exactly the unchanged production processor output.
        assert np.array_equal(np.asarray(views[0][1]), process_image(raw, 'head', cfg))
        source = dict(episode=root.name, raw_row=index, observation_id=row['observation_id'],
                      seconds_from_raw_start=(row['available_monotonic_ns']-rows[0]['available_monotonic_ns'])/1e9,
                      frame=f, source_payload_sha256_verified=True)
        manifest['sources'].append(source)
        cache[key] = (source, raw, views)
        return cache[key]

    def grid(records, title, filename, compact=False):
        image_size = 224 if compact else 336
        col_w = image_size + 30
        left = 150
        row_h = image_size + 42
        width, height = left + 3*col_w + 20, 174 + len(records)*row_h + 46
        canvas = Image.new('RGB', (width, height), '#eef2f7')
        draw = ImageDraw.Draw(canvas)
        label(draw, (20, 15), title, 27, bold=True)
        label(draw, (20, 56), '同一原图 · 同一缩放算法 · 无随机增强；每张均为实际224×224模型像素', 18)
        for col, var in enumerate(variants):
            x = left + col*col_w
            label(draw, (x, 91), f'{var["id"]}  {var["name"]}', 23, bold=True)
            label(draw, (x, 125), f'有效{var["content_size"][0]}×{var["content_size"][1]} / {var["linear_scale_vs_A"]:.3f}×', 17)
        for n, (source, raw, views) in enumerate(records):
            y = 169 + n*row_h
            label(draw, (14, y+image_size//2-35), f'{source["seconds_from_raw_start"]:.2f}s\n原始行{source["raw_row"]}', 20)
            for col, (_, current) in enumerate(views):
                canvas.paste(current.resize((image_size,image_size), Image.Resampling.NEAREST), (left+col*col_w,y))
        label(draw, (20, height-34), '黑边是处理器补边；物体未做展示放大以外的修饰。B/C为候选，未替换当前ROI。', 17)
        canvas.save(out / filename)

    def detail(record, title, filename):
        source, raw, views = record
        canvas = Image.new('RGB', (1536, 968), '#eef2f7')
        draw = ImageDraw.Draw(canvas)
        label(draw, (24, 17), title, 30, bold=True)
        label(draw, (24, 62), f'原始行{source["raw_row"]} / {source["seconds_from_raw_start"]:.3f}s；上排为裁剪画面，下排为实际模型输入', 22)
        for col, (var, (cropped, current)) in enumerate(zip(variants, views)):
            x=col*512
            label(draw,(x+20,105), f'{var["id"]}  {var["name"]}  {tuple(var["box"])}',21,bold=True)
            preview=cropped.copy()
            preview.thumbnail((480,376),Image.Resampling.LANCZOS)
            canvas.paste(preview,(x+(512-preview.width)//2,150+(376-preview.height)//2))
            label(draw,(x+20,548), f'224×224输入；相对物体尺度 {var["linear_scale_vs_A"]:.3f}×',21)
            canvas.paste(current.resize((336,336),Image.Resampling.NEAREST),(x+88,590))
        label(draw,(24,938),'候选比较，尚未部署；独立PNG保留原始裁剪分辨率和精确224像素。',18)
        canvas.save(out/filename)

    key = [get_record(v1,vrows,i,'v1') for i in key_rows]
    hist = [get_record(history,hrows,i,'history') for i in (450,540)]
    grid(key,'V1关键时刻：当前ROI与两个候选','v1-model-comparison.png')
    grid(hist,'历史抬升时刻：检查顶部余量','history-model-comparison.png')
    detail(key[1],'V1抓住木块：裁剪画面 + 模型输入','v1-crops-comparison.png')
    detail(key[2],'V1右移极值：检查右侧余量','v1-right-edge-comparison.png')
    detail(hist[0],'历史抬升极值：检查顶部余量','history-top-edge-comparison.png')
    indices=sorted(set(range(0,len(vrows),40))|{len(vrows)-1}|set(key_rows))
    for start in range(0,len(indices),10):
        recs=[get_record(v1,vrows,i,'v1') for i in indices[start:start+10]]
        grid(recs,f'V1全段均匀抽查 {start//10+1}','contact-%02d.png'%(start//10),compact=True)
    manifest['v1_uniform_review_rows']=indices
    manifest['checks']={'production_A_pixel_equivalent':True,'payload_sha256_verified':True,
                        'production_config_and_processor_unchanged':all(sha256(Path(p))==h for p,h in before.items())}
    assert manifest['checks']['production_config_and_processor_unchanged']
    for p in sorted(out.rglob('*.png')):
        manifest['files'][str(p.relative_to(out))]={'sha256':sha256(p),'bytes':p.stat().st_size}
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'output':str(out),'v1_sampled_frames':len(indices),'historical_frames':2,
                      'png_files':len(manifest['files']),'checks':manifest['checks']},ensure_ascii=False))


if __name__=='__main__':
    main()

#!/usr/bin/env python3
"""Build the human-facing video review page without changing any annotations."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import re
import shutil


TASK_ROOT = Path(__file__).resolve().parents[1]
BACKUP_NAME = "index.before_cycle_review_20260914.html"
LEGACY_VIDEOS = (
    "all_data_derived/episode_000000",
    "all_data_1_derived/episode_000000",
    "all_data_2_derived/episode_000117",
    "all_data_3_derived/episode_000198",
    "all_data_2_derived/episode_000256",
    "all_data_2_derived/episode_000055",
)


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def stamp(seconds: float) -> str:
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes):02d}:{rest:05.2f}"


def marker(start: float, end: float | None, label: str, kind: str = "point") -> str:
    end_attr = f' data-end="{end:.9f}"' if end is not None else ""
    return (f'<button type="button" class="seek {kind}" data-start="{start:.9f}"'
            f'{end_attr}>{esc(label)}</button>')


def points(record: dict) -> list[str]:
    """Use existing findings as review aids, never as automatic acceptance."""
    found = []
    for when in record.get("visible_human_replenishment_examples_s", []):
        found.append(marker(max(0, when - 3), when + 3,
                            f"人工补料附近 · {when:g} 秒"))
    near = record.get("human_near_gripper_evidence")
    if near:
        when = near["log_time_s"]
        found.append(marker(max(0, when - 3), when + 4,
                            f"人工接近右夹爪 · {when:.2f} 秒"))
    for item in record.get("missing_base_motion_evidence", []):
        start, end = item["rough_visual_interval_s"]
        found.append(marker(start, end, f"画面移动、底盘标签为零 · {start:g}–{end:g} 秒"))
    mismatch = record.get("label_mismatch", {})
    for key, label in (("outbound_window_s", "去程标签与画面返程冲突"),
                       ("return_window_s", "返程标签与画面去程冲突")):
        if key in mismatch:
            start, end = mismatch[key]
            found.append(marker(start, end, f"{label} · {start:g}–{end:g} 秒"))
    if record["episode"] == "all_data_2_derived/episode_000055":
        found.append(marker(31, 36, "反馈断档附近 · 32.18–34.78 秒"))
    if record["episode"] == "all_data_derived/episode_000008":
        found.append(marker(61, 69, "末段有人入画 · 63.5–66.5 秒"))
    if record["episode"].endswith(("episode_000062", "episode_000151")):
        found.append(marker(0, 5, "开头相机有效画面 · 0–5 秒"))
    return found


def selector(field: str, label: str, options: tuple[str, ...], number: int) -> str:
    options_html = '<option value="">尚未判断</option>' + "".join(
        f'<option>{esc(value)}</option>' for value in options
    )
    ident = f"{field}-{number}"
    return (f'<label for="{ident}">{esc(label)}<select id="{ident}" '
            f'data-field="{field}">{options_html}</select></label>')


def card(number: int, annotation: dict, record: dict) -> str:
    episode = annotation["episode"]
    if not re.fullmatch(r"all_data(?:_[123])?_derived/episode_\d{6}", episode):
        raise ValueError(f"Unexpected episode identifier: {episode!r}")
    stem = episode.replace("/", "_")
    candidates = []
    for index, cycle in enumerate(annotation["cycles"], 1):
        start, end = (value / 1e9 for value in cycle["interval_ns"])
        candidates.append(marker(start, end,
                                 f"候选 {index} · {stamp(start)}–{stamp(end)} · 播放区间",
                                 "candidate"))
    exclusions = []
    for interval in annotation.get("exclusions", []):
        start, end = (value / 1e9 for value in interval["interval_ns"])
        exclusions.append(marker(start, end,
                                 f"待查等待/人工补料 · {start:g}–{end:g} 秒"))
    badge = ("已确认底盘标签有误：看完视频也不能直接恢复训练"
             if record.get("base_label_supervision_requires_reannotation")
             else "候选待确认 · 尚未验收")
    key_points = points(record)
    highlight = ('<div class="points"><strong>重点查看</strong><div class="buttons">'
                 + "".join(key_points) + '</div></div>') if key_points else ""
    return f"""<article id="review-{number}" data-episode="{esc(episode)}" data-number="{number}">
  <div class="card-heading"><span class="number">{number:02d}</span><div><h2>{esc(episode)}</h2>
  <p class="muted">完整录制 {record['duration_s']:.2f} 秒 · 粗审观察 {record['visually_observed_transport_cycles']} 轮 · {len(annotation['cycles'])} 个候选区间</p></div></div>
  <p class="badge{' bad' if record.get('base_label_supervision_requires_reannotation') else ''}">{badge}</p>
  <p class="finding"><strong>已有粗审结果：</strong>{esc(record['summary'])}</p>
  <video controls preload="none" playsinline poster="{stem}.jpg" src="{stem}.mp4" aria-label="第{number}条完整三路视频"></video>
  <p class="play-status muted" aria-live="polite">尚未播放；候选区间按钮会在区间结束时暂停。</p>
  <div class="buttons"><button type="button" class="full">从头播放完整录制</button><button type="button" class="resume">继续播放，不限区间</button><a href="{stem}.mp4" download>下载完整 MP4</a></div>
  <div class="candidates"><strong>待确认区间（粗边界，可改）</strong><div class="buttons">{''.join(candidates)}</div></div>
  {highlight}
  {'<div class="buttons">' + ''.join(exclusions) + '</div>' if exclusions else ''}
  <div class="feedback-fields">
    <label for="count-{number}">你实际看到几轮<input id="count-{number}" data-field="count" type="text" inputmode="numeric" placeholder="例如：1 或 2"></label>
    {selector('outcome', '任务结果', ('全部成功', '部分成功（备注说明）', '全部失败', '看不清/无法判断'), number)}
    {selector('human', '人工干预', ('未见干预', '仅有人入画，未见干预', '有干预（备注写时间）', '看不清/无法判断'), number)}
    <label class="wide" for="keep-{number}">建议保留的时间段 / 整条排除<input id="keep-{number}" data-field="keep" type="text" placeholder="例如：0–31秒、45–76秒；或整条排除；不确定可空着"></label>
    <label class="wide" for="notes-{number}">备注<textarea id="notes-{number}" data-field="notes" rows="2" placeholder="例如：第2轮没抓稳；51秒有人调整袋口。时间以本条完整视频为准。"></textarea></label>
  </div>
</article>"""


CSS = """
:root{color-scheme:dark;font:16px/1.6 system-ui,-apple-system,'Segoe UI',sans-serif;background:#0b1420;color:#eaf0f7}
*{box-sizing:border-box}body{margin:0}main{max-width:1500px;margin:auto;padding:32px 24px 96px}h1{font-size:clamp(25px,4vw,38px);line-height:1.3;margin:8px 0 16px}h2{font-size:20px;overflow-wrap:anywhere;margin:0}p{margin:10px 0}a{color:#87c9ff}button,a,input,select,textarea{touch-action:manipulation}button{cursor:pointer;background:#20364f;color:#f4f8ff;border:1px solid #58718d;border-radius:7px;padding:8px 12px;font:inherit;font-size:14px}button:hover{background:#2c496c}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:3px solid #6fc1ff;outline-offset:3px}.candidate{border-color:#57a7e5;background:#193958}.buttons{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:9px 0}.muted{color:#aabbd0;font-size:14px}.intro,.feedback-box{background:#152437;border:1px solid #36506e;border-radius:12px;padding:18px 22px}.intro strong{color:#b9dfff}.nav{position:sticky;top:0;z-index:5;background:#0b1420ef;backdrop-filter:blur(6px);border-bottom:1px solid #38516d;padding:9px 0;margin:20px 0}.nav a{display:inline-block;text-decoration:none;padding:4px 11px;border:1px solid #3c5775;border-radius:6px}.nav .feedback-link{margin-left:auto}article{scroll-margin-top:100px;margin:26px 0;padding:22px;background:#132133;border:1px solid #344a66;border-radius:12px}.card-heading{display:flex;gap:14px;align-items:flex-start}.number{font-size:26px;font-weight:700;background:#274665;padding:0 11px;border-radius:8px}.badge{display:inline-block;font-size:13px;border:1px solid #8b7b37;background:#3a331a;color:#f2d987;padding:3px 9px;border-radius:5px}.badge.bad{border-color:#a35d66;background:#402630;color:#ffc0c5}.finding{margin:10px 0 16px}video{display:block;width:100%;aspect-ratio:1920/460;background:#000;object-fit:contain;border-radius:5px}.points,.candidates{margin:18px 0 10px}.feedback-fields{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;border-top:1px solid #344a66;margin-top:20px;padding-top:16px}label{display:block;font-size:14px;color:#bfd0e5}input,select,textarea{display:block;width:100%;margin-top:5px;background:#0c1827;border:1px solid #4a6280;border-radius:6px;padding:9px 10px;color:#eaf0f7;font:inherit}textarea{resize:vertical}.wide{grid-column:1/-1}.feedback-box{scroll-margin-top:100px;margin-top:32px}#feedback-output{min-height:220px;font:14px/1.65 ui-monospace,monospace;white-space:pre-wrap}details{margin-top:30px}summary{cursor:pointer}.legacy{display:flex;flex-direction:column;gap:8px;padding:15px 0}noscript p{border:1px solid #a98140;padding:12px}footer{margin-top:26px;color:#aabbd0;font-size:13px}@media(max-width:720px){main{padding:20px 12px 70px}article{padding:15px}.feedback-fields{grid-template-columns:1fr}.nav .feedback-link{margin-left:0}.intro,.feedback-box{padding:15px}video{aspect-ratio:1920/540}h2{font-size:17px}.number{font-size:22px}}
"""


JS = r"""
'use strict';
const config = JSON.parse(document.getElementById('review-config').textContent);
const articles = Array.from(document.querySelectorAll('article[data-episode]'));
const status = document.getElementById('draft-status');
const output = document.getElementById('feedback-output');
let storageWorks = true;
let saved = {};
try { saved = JSON.parse(localStorage.getItem(config.storageKey) || '{}') || {}; }
catch (_) { storageWorks = false; }
const draftText = () => storageWorks
  ? '草稿仅保存在当前浏览器，尚未提交。复制后发到对话里，我再整理清单。'
  : '浏览器未能保存草稿；本页填写内容尚未提交，请在关闭前复制反馈。';
function values(article) {
  const result = {};
  article.querySelectorAll('[data-field]').forEach(field => { result[field.dataset.field] = field.value.trim(); });
  return result;
}
function refreshFeedback() {
  const lines = ['F1 泡沫分拣：8条视频复核反馈', '时间为各完整视频的相对秒数；以下是观看反馈，未代替接口、时钟和底盘标签验收。'];
  articles.forEach(article => {
    const data = values(article);
    lines.push('', `${article.dataset.number}. ${article.dataset.episode}`);
    if (!Object.values(data).some(Boolean)) { lines.push('尚未反馈'); return; }
    lines.push(`实际轮数：${data.count || '未填'}`, `任务结果：${data.outcome || '未判断'}`,
      `人工干预：${data.human || '未判断'}`, `建议保留区间/排除：${data.keep || '未填'}`,
      `备注：${data.notes || '无'}`);
  });
  output.value = lines.join('\n');
}
function saveDraft() {
  const draft = {};
  articles.forEach(article => { draft[article.dataset.episode] = values(article); });
  try { localStorage.setItem(config.storageKey, JSON.stringify(draft)); storageWorks = true; }
  catch (_) { storageWorks = false; }
  status.textContent = draftText();
  refreshFeedback();
}
articles.forEach(article => {
  const previous = saved[article.dataset.episode];
  article.querySelectorAll('[data-field]').forEach(field => {
    if (previous && typeof previous[field.dataset.field] === 'string') field.value = previous[field.dataset.field];
    field.addEventListener('input', saveDraft);
    field.addEventListener('change', saveDraft);
  });
  const video = article.querySelector('video');
  const playStatus = article.querySelector('.play-status');
  let stopAt = null;
  let requestId = 0;
  async function start(startAt, endAt) {
    const request = ++requestId;
    stopAt = endAt;
    articles.forEach(other => { if (other !== article) other.querySelector('video').pause(); });
    if (!video.readyState) {
      video.preload = 'metadata';
      video.load();
      await new Promise(resolve => {
        const finish = () => {
          video.removeEventListener('loadedmetadata', finish);
          video.removeEventListener('error', finish);
          resolve();
        };
        video.addEventListener('loadedmetadata', finish, {once:true});
        video.addEventListener('error', finish, {once:true});
      });
    }
    if (request !== requestId || video.error) return;
    if (startAt !== null) video.currentTime = Math.max(0, Math.min(startAt, video.duration || startAt));
    playStatus.textContent = endAt === null ? '播放完整录制，不限区间。' : `播放候选/检查区间，到 ${endAt.toFixed(2)} 秒暂停；这不是正式裁剪。`;
    try { await video.play(); }
    catch (_) { playStatus.textContent = '已定位，请点击视频中的播放按钮。'; }
  }
  article.querySelectorAll('.seek').forEach(button => button.addEventListener('click', () => {
    start(Number(button.dataset.start), button.dataset.end === undefined ? null : Number(button.dataset.end));
  }));
  article.querySelector('.full').addEventListener('click', () => start(0, null));
  article.querySelector('.resume').addEventListener('click', () => start(null, null));
  video.addEventListener('timeupdate', () => {
    if (stopAt !== null && video.currentTime >= stopAt) {
      video.pause();
      const end = stopAt;
      stopAt = null;
      playStatus.textContent = `区间在 ${end.toFixed(2)} 秒结束；可继续播放或选择其他区间。`;
    }
  });
  video.addEventListener('error', () => {
    playStatus.textContent = '视频暂时未能加载。请刷新重试，或点击“下载完整 MP4”检查文件入口。';
  });
});
document.getElementById('copy-feedback').addEventListener('click', async () => {
  refreshFeedback();
  try {
    await navigator.clipboard.writeText(output.value);
    status.textContent = '反馈已复制，尚未提交；请粘贴发到对话里。';
  } catch (_) {
    output.focus(); output.select();
    status.textContent = '反馈已选中，请按 Ctrl+C（Mac：⌘C）复制，再粘贴发到对话里。尚未提交。';
  }
});
status.textContent = draftText();
refreshFeedback();
"""


def build(task: Path, output: Path) -> dict:
    annotation_root = task / "results/cleaning_v2_review/annotations"
    findings_path = task / "results/cleaning_v1/cycle_review/findings.json"
    findings = json.loads(findings_path.read_text(encoding="utf-8"))
    by_episode = {item["episode"]: item for item in findings["records"]}
    selected = []
    fingerprint = hashlib.sha256(findings_path.read_bytes())
    for path in sorted(annotation_root.glob("*/*.json")):
        raw = path.read_bytes()
        annotation = json.loads(raw)
        if annotation.get("cycles"):
            if annotation["episode"] not in by_episode:
                raise ValueError(f"No reviewed finding for {annotation['episode']}")
            selected.append(annotation)
            fingerprint.update(raw)
    if not selected:
        raise ValueError("No candidate cycles found; refusing to replace the page")
    cycle_count = sum(len(item["cycles"]) for item in selected)
    cards = "\n".join(card(number, item, by_episode[item["episode"]])
                      for number, item in enumerate(selected, 1))
    nav = "".join(f'<a href="#review-{number}">{number}</a>'
                  for number in range(1, len(selected) + 1))
    legacy = "".join(f'<a href="{episode.replace("/", "_")}.mp4">{esc(episode)} · 完整 MP4</a>'
                     for episode in LEGACY_VIDEOS)
    config = json.dumps({"storageKey": "f1-cycle-review-" + fingerprint.hexdigest()[:20]},
                        ensure_ascii=False).replace("<", "\\u003c")
    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>F1 泡沫分拣 · {len(selected)}条循环复核</title><style>{CSS}</style></head>
<body><main><header><p class="muted">F1 · 泡沫分拣 · 循环复核</p><h1>{len(selected)}条完整视频，{cycle_count}个待确认区间</h1>
<div class="intro"><p><strong>请看每条有几轮、每轮是否成功、有无人工干预，再告诉我建议保留哪段。</strong>可以直接在对话里按编号回复，也可以在下面记备注后复制反馈。</p>
<p>现有结论来自抽帧粗审，候选区间不是已验收切片；你可以改边界或建议整条排除。默认没有任何条目被选为通过。</p>
<p class="muted">三路画面：左为头部、中为左腕、右为右腕。15 fps 预览，默认1倍速，保留完整录制时间轴，无音频。按 MCAP log 时间使用不晚于当前时刻的最近帧；缺帧、旧帧提示保留。视频观看用于确认任务内容；相机曝光对时、接口及底盘标签由我们另外核查。</p></div></header>
<nav class="nav buttons" aria-label="视频导航"><span>跳到：</span>{nav}<a class="feedback-link" href="#feedback">复制反馈 ↓</a></nav>
<noscript><p>当前浏览器禁用了 JavaScript。完整视频仍可播放；区间跳转和草稿功能需要启用 JavaScript。也可直接按1–{len(selected)}号在对话里回复。</p></noscript>
{cards}
<section class="feedback-box" id="feedback"><h2>把观看结果发给我</h2><p>只需反馈实际看见的情况；不确定就写不确定，无需填写接口或时钟检查表。</p><p id="draft-status" class="muted" role="status"></p>
<div class="buttons"><button type="button" id="copy-feedback">复制全部反馈</button></div><label for="feedback-output">反馈文本（复制后粘贴到对话里）</label><textarea id="feedback-output" readonly></textarea></section>
<details><summary>原来6条视频入口（保留）</summary><div class="legacy"><a href="{BACKUP_NAME}">打开原6条视频页面</a>{legacy}</div></details>
<footer>来源：cleaning_v2_review/annotations 的候选时间区间，以及 cleaning_v1/cycle_review/findings.json 的粗审结论。此页仅供观看与反馈，不会修改原始数据、标注文件或训练通过状态。</footer>
</main><script id="review-config" type="application/json">{config}</script><script>{JS}</script></body></html>
"""
    output.parent.mkdir(parents=True, exist_ok=True)
    backup = output.with_name(BACKUP_NAME)
    if output.exists() and not backup.exists():
        # Exclusive creation preserves the actual old page even on a concurrent run.
        with backup.open("xb") as handle, output.open("rb") as source:
            shutil.copyfileobj(source, handle)
    temporary = output.with_suffix(".html.tmp")
    temporary.write_text(page, encoding="utf-8")
    temporary.replace(output)
    return {"output": str(output), "original_page_backup": str(backup),
            "recordings": len(selected), "candidate_intervals": cycle_count,
            "annotations_modified": False,
            "episodes": [item["episode"] for item in selected]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-root", type=Path, default=TASK_ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or args.task_root / "results/20260914/videos/index.html"
    print(json.dumps(build(args.task_root, output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

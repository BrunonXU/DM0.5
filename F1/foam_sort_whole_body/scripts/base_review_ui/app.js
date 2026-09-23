'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const video = $('review-video');
  const NS = 'http://www.w3.org/2000/svg';
  const COLORS = { vx:'#7bc7ff', vy:'#7bdfbc', wz:'#cbb4ff', motion:'#ffbd70', lead:'#7bc7ff', hal:'#ffbd70' };
  const ANCHORS = [
    ['grasp_complete','抓取完成'], ['outbound_start','去程开始'], ['outbound_stop','去程停止'],
    ['place_complete','放置完成'], ['return_start','返程开始'], ['return_stop','返程停止']
  ];
  const ANCHOR_HELP = {
    grasp_complete:'画面中物料已抓稳的时刻；与底盘启停分别标记，允许动作重叠。',
    outbound_start:'取料侧底盘首次后退的起点，不是后退完成后右移的起点。',
    outbound_stop:'到达放料位置最终停稳的时刻；按实际移动段记录，不强制凑齐三段，中间停顿另加事件。',
    place_complete:'画面中物料已放妥并释放的时刻；不能仅用夹爪阈值认定。',
    return_start:'放料侧底盘首次后退的起点，不是后退完成后左移的起点。',
    return_stop:'返回取料位置最终停稳的时刻；按实际移动段记录，不强制凑齐三段，中间停顿另加事件。'
  };
  const CATEGORY = {
    confirmed_zero_motion:'已确认的错配样本', zero_motion_review:'零速 / 画面运动疑点',
    nonzero_low_motion_review:'非零速 / 低运动疑点', no_priority_review:'未命中主要疑点 · 对照样本'
  };
  let manifest = null, selected = null, data = null, draft = null, relabelPreviews = [];
  let selectionToken = 0, fetchController = null, pendingSeek = null, focusEnd = null, hasViewedFrame = false;
  let chartMeta = [], activeRange = null, lastNearRedraw = -Infinity, frameRequest = null;
  const phaseEditor = window.createBasePhaseEditor({container:$('phase-editor'), seek:t=>{focusEnd=null;seekTo(t);}, currentTime, frameReady:()=>hasViewedFrame && pendingSeek===null && video.readyState>=2 && !video.seeking});

  function text(id, value) { $(id).textContent = value; }
  function node(tag, attrs = {}, content = '') {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
    el.textContent = content;
    return el;
  }
  function svgNode(tag, attrs = {}, content = '') {
    const el = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
    el.textContent = content;
    return el;
  }
  function finite(value) { return typeof value === 'number' && Number.isFinite(value); }
  function fmt(value, places = 3) { return finite(value) ? value.toFixed(places) : '—'; }
  function assetURL(value) {
    if (typeof value !== 'string' || !value) throw new Error('缺少媒体或数据地址');
    const url = new URL(value, location.href);
    if (url.origin !== location.origin || !['http:', 'https:'].includes(url.protocol)) throw new Error('资源地址必须来自本页服务器');
    return url.href;
  }
  function currentTime() { return finite(video.currentTime) ? Math.min(duration(),video.currentTime) : 0; }
  function duration() { return selected ? Number(selected.source_duration_s) || 0 : 0; }
  function lastIndex(times, target) {
    let lo = 0, hi = times.length;
    while (lo < hi) { const mid = (lo + hi) >>> 1; if (times[mid] <= target + 1e-8) lo = mid + 1; else hi = mid; }
    return lo - 1;
  }
  function valueAt(key, column, index) {
    if (!data || index < 0 || !data.numeric_valid?.[key]?.[index]) return null;
    const value = data.numeric?.[key]?.[index]?.[column];
    return finite(value) ? value : null;
  }
  function showError(message) { text('global-error', message); $('global-error').classList.remove('hidden'); }
  function showVideoError(message) { text('video-error', message); $('video-error').classList.remove('hidden'); }
  function clearErrors() { $('global-error').classList.add('hidden'); $('video-error').classList.add('hidden'); }
  function episodeStatus(episode) {
    if (episode.status === 'excluded_confirmed_base_timing') return '已从候选排除 · 尚未修复';
    return episode.status_label || CATEGORY[episode.category] || '待审核';
  }

  function buildList() {
    const list = $('episode-list'); list.replaceChildren();
    text('episode-count', `${manifest.episodes.length} 份`);
    manifest.episodes.forEach((episode, index) => {
      const button = node('button', {type:'button', class:'episode-button', 'data-episode-id':episode.id, 'aria-pressed':'false'});
      const short = episode.episode?.split('/').pop() || episode.id;
      button.append(node('strong', {}, `${String(index + 1).padStart(2,'0')}  ${short}`));
      button.append(node('span', {class:'episode-sub'}, `${episode.episode?.split('/')[0] || ''} · ${fmt(episode.source_duration_s,1)} 秒`));
      button.append(node('span', {class:`list-category${episode.category === 'no_priority_review' ? ' neutral' : ''}`}, episodeStatus(episode)));
      button.addEventListener('click', () => selectEpisode(episode));
      list.append(button);
    });
  }

  function validateData(value, episode) {
    if (!value || value.episode !== episode.episode || !Array.isArray(value.time_s) || !value.time_s.length) throw new Error('时间轴或录制身份不匹配');
    if (!value.time_s.every((t,i) => finite(t) && (i === 0 || t > value.time_s[i-1]))) throw new Error('时间轴不是严格递增的原录制秒');
    for (const key of ['hal','lead','base','gripper_command','gripper_feedback']) {
      if (!Array.isArray(value.numeric?.[key]) || value.numeric[key].length !== value.time_s.length || !Array.isArray(value.numeric_valid?.[key]) || value.numeric_valid[key].length !== value.time_s.length) throw new Error(`同步数据缺失：${key}`);
    }
    return value;
  }

  async function selectEpisode(episode) {
    const token = ++selectionToken;
    fetchController?.abort(); fetchController = new AbortController();
    video.pause(); video.removeAttribute('src'); video.preload = 'none'; video.load();
    pendingSeek = null; focusEnd = null; hasViewedFrame = false; data = null; selected = episode; draft = null;
    phaseEditor.clear();text('candidate-readout','');$('candidate-legend').classList.add('hidden');text('candidate-legend','虚线：新建议 vx（粉）/ vy（黄）/ wz（橙）；待确认，空白为未知');
    chartMeta = []; activeRange = null; $('base-chart').replaceChildren(); $('arm-chart').replaceChildren();
    $('anchor-fields').replaceChildren(); $('custom-events').replaceChildren();
    $('review-notes').value = ''; $('review-notes').disabled = true; $('feedback-output').value = ''; $('download-video').removeAttribute('href');
    $('copy-fallback').classList.add('hidden'); text('export-status','');
    clearErrors();
    for (const button of $('episode-list').children) {
      const active = button.dataset.episodeId === episode.id;
      button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active));
    }
    text('episode-title', episode.title || episode.episode);
    text('episode-path', episode.episode);
    text('episode-category', CATEGORY[episode.category] || '审核样本');
    text('source-status', episodeStatus(episode));
    text('sample-notice', episode.status === 'excluded_confirmed_base_timing'
      ? '此条原图已确认底盘阶段冲突，已从训练候选中排除。原数据保留，标签尚未修复；这里继续展示供诊断和重建核对。'
      : manifest.notice || '自动疑点是复核线索；请结合固定背景、手臂和夹爪，标记实际阶段。');
    text('current-time', '0.000'); text('total-time', fmt(duration(),3));
    text('play-status','正在加载同步数据；视频保持暂停。'); text('load-status','正在加载当前录制的同步数据…'); $('load-status').dataset.state = 'loading';
    $('copy-feedback').disabled = true; $('download-feedback').disabled = true;
    $('play-pause').disabled = true; $('back-one').disabled = true; $('forward-one').disabled = true; $('add-custom').disabled = true;
    $('focus-buttons').replaceChildren();
    for (const key of ['vx-value','vy-value','wz-value']) text(key,'—');
    text('motion-status','背景：—'); text('joint-readout','—');
    try {
      video.poster = assetURL(episode.poster_url);
      const response = await fetch(assetURL(episode.data_url), {signal:fetchController.signal, cache:'no-cache'});
      if (!response.ok) throw new Error(`同步数据请求失败（HTTP ${response.status}）`);
      const loaded = validateData(await response.json(), episode);
      if (token !== selectionToken) return;
      data = loaded;
      video.src = assetURL(episode.video_url); video.preload = 'none'; video.load();
      video.playbackRate = Number($('playback-speed').value); video.pause();
      $('download-video').href = assetURL(episode.video_url);
      loadDraft(); buildAnchors(); buildFocusButtons(); drawCharts(); updateTime();
      phaseEditor.setSource({episode:selected.episode,source_origin_ns:data.source_origin_ns,source_duration_s:duration(),provenance:data.provenance||{}});
      loadRelabelPreview(token, selected.episode);
      $('copy-feedback').disabled = false; $('download-feedback').disabled = false; $('review-notes').disabled = false;
      $('play-pause').disabled = false; $('back-one').disabled = false; $('forward-one').disabled = false; $('add-custom').disabled = false;
      text('play-status','当前为封面预览，不对应下方0秒；播放或定位载入实际画面后再点“此刻”。所有时间均为原录制秒。');
      const method = data.provenance?.label_method ?? data.label_method;
      const confidence = data.provenance?.label_confidence ?? data.label_confidence;
      const methodLabel = typeof method === 'string' ? ` 原补标方法：${method}${finite(confidence) ? `，置信度 ${confidence.toFixed(3)}` : ''}。` : '';
      text('load-status', `已载入 ${data.time_s.length} 个同步显示时刻。原补标先估计去程 / 返程两段窗口，再套六段路线；应核对固定背景，判断手部动作是否影响窗口。${methodLabel} 人工标记仅作重建候选。`);
      $('load-status').dataset.state = 'ready';
    } catch (error) {
      if (token !== selectionToken || error.name === 'AbortError') return;
      showError(`当前录制加载失败：${error.message}。请刷新或选择其他录制。`);
      text('load-status','同步数据未载入，当前不能标记或导出。'); $('load-status').dataset.state = 'error';
    }
  }

  function buildFocusButtons() {
    const container = $('focus-buttons'); container.replaceChildren();
    const focus = selected.focus_interval;
    const intervals = [];
    if (focus && finite(focus.start_s) && finite(focus.end_s)) intervals.push({...focus, name:'主要疑点'});
    for (const item of data.review_intervals || []) {
      if (!finite(item.start_s) || !finite(item.end_s)) continue;
      if (intervals.some(v => Math.abs(v.start_s-item.start_s)<.05 && Math.abs(v.end_s-item.end_s)<.05)) continue;
      if (intervals.length >= 3) break;
      intervals.push({...item, name:item.kind?.includes('nonzero') ? '非零速 / 低运动' : '其他疑点'});
    }
    intervals.forEach(item => {
      const button = node('button',{type:'button', 'data-action':'focus-interval', 'data-start':item.start_s, 'data-end':item.end_s},`${item.name} ${fmt(item.start_s,1)}–${fmt(item.end_s,1)}秒 · 前后 2 秒`);
      button.addEventListener('click', () => { focusEnd = Math.min(duration(), item.end_s+2); seekTo(Math.max(0,item.start_s-2)); text('play-status',`已定位到疑点前 2 秒；点播放查看，至 ${fmt(focusEnd,2)} 秒自动暂停。`); });
      container.append(button);
    });
    const full = node('button',{type:'button','data-action':'full-recording'},'从头看完整录制');
    full.addEventListener('click', () => { focusEnd = null; seekTo(0); text('play-status','已回到开头；点播放查看完整录制。'); });
    container.append(full);
  }

  function seekTo(time) {
    if (!data) return;
    const target = Math.max(0, Math.min(Math.max(0,duration()-0.001), time));
    if (video.readyState < 1) {
      pendingSeek = target; video.preload = 'metadata'; video.load();
      text('current-time',fmt(target));
      text('play-status','正在载入所选时间的画面…');
    } else { video.currentTime = target; updateTime(); }
  }
  function togglePlay() {
    if (!data) return;
    if (video.paused) video.play().catch(error => {
      if (error.name !== 'AbortError') text('play-status',`播放未开始：${error.message}。可使用视频自带播放按钮重试。`);
    }); else video.pause();
  }

  async function loadRelabelPreview(token, episode) {
    const entry=relabelPreviews.find(r=>r.episode===episode);if(!entry)return;
    try {
      const response=await fetch(assetURL(entry.url),{cache:'no-cache'});if(!response.ok)throw new Error('候选读取失败');
      const preview=await response.json();if(token!==selectionToken)return;
      if(preview.episode!==episode||preview.source_origin_ns!==data.source_origin_ns||preview.time_s.length!==data.time_s.length||preview.time_s.some((t,i)=>Math.abs(t-data.time_s[i])>1e-9)||preview.base_v.length!==data.time_s.length)throw new Error('候选时间轴不匹配');
      data.relabel_candidate=preview;
      phaseEditor.setProposal(preview.phase,preview.phase_sha256);
      text('candidate-legend',preview.review_notice || '虚线：新建议 vx（粉）/ vy（黄）/ wz（橙）；候选未完成训练验收，空白为未知');
      $('candidate-legend').classList.remove('hidden');drawCharts();updateTime();
    } catch(error){if(token===selectionToken){text('candidate-legend',`修订候选未载入：${error.message}`);$('candidate-legend').classList.remove('hidden');}}
  }

  function numericSeries(key, col, color, label) {
    return { label, color, times:data.time_s, values:data.time_s.map((_,i) => valueAt(key,col,i)) };
  }
  function limits(series, minimumSpan = .1) {
    let lo = Infinity, hi = -Infinity;
    for (const item of series) for (let i=0; i<item.values.length; i++) {
      if (item.times[i] < activeRange[0] || item.times[i] > activeRange[1]) continue;
      const value = item.values[i]; if (finite(value)) { lo=Math.min(lo,value); hi=Math.max(hi,value); }
    }
    if (!finite(lo)) return [-minimumSpan, minimumSpan];
    const span = Math.max(hi-lo,minimumSpan); return [lo-span*.15,hi+span*.15];
  }
  function getRange() {
    const total = Math.max(duration(), .1);
    if ($('chart-window').value === 'near') {
      const start = Math.max(0,Math.min(total-12,currentTime()-6));
      return [start,Math.min(total,start+12)];
    }
    return [0,total];
  }
  function plotTrack(svg, options) {
    const {top,bottom,unit,series,lo,hi,motion=false} = options;
    const left=62,right=840,x=t=>left+(t-activeRange[0])/(activeRange[1]-activeRange[0])*(right-left);
    const y=v=>bottom-(v-lo)/(hi-lo)*(bottom-top);
    const clipId=`clip-${svg.id}-${top}`;
    const defs=svgNode('defs');const clip=svgNode('clipPath',{id:clipId});clip.append(svgNode('rect',{x:left,y:top,width:right-left,height:bottom-top}));defs.append(clip);svg.append(defs);
    const layer=svgNode('g',{'clip-path':`url(#${clipId})`});svg.append(layer);
    for (const window of data.label_windows || []) {
      if (!finite(window.start_s) || !finite(window.end_s)) continue;
      layer.append(svgNode('rect',{x:x(window.start_s),y:top,width:Math.max(0,x(window.end_s)-x(window.start_s)),height:bottom-top,fill:'#36516b',opacity:.28}));
    }
    if (!motion) for (const interval of data.review_intervals || []) {
      if (!finite(interval.start_s) || !finite(interval.end_s)) continue;
      layer.append(svgNode('rect',{x:x(interval.start_s),y:top,width:Math.max(0,x(interval.end_s)-x(interval.start_s)),height:bottom-top,fill:'#ad752c',opacity:.15}));
    }
    for (let i=0;i<=2;i++) {
      const value=lo+(hi-lo)*i/2, yy=y(value);
      svg.append(svgNode('line',{x1:left,x2:right,y1:yy,y2:yy,stroke:'#263b4d','stroke-width':1}));
      svg.append(svgNode('text',{x:left-8,y:yy+4,'text-anchor':'end',fill:'#9db3c5','font-size':10},fmt(value,Math.abs(hi-lo)<.1?3:Math.abs(hi-lo)>20?0:2)));
    }
    svg.append(svgNode('text',{x:8,y:top-7,fill:'#becfde','font-size':11},unit));
    if (motion) {
      for (const pair of data.motion_pairs || []) {
        if (!finite(pair.start_s) || !finite(pair.end_s)) continue;
        if (pair.status === 'uncertain' || pair.evidence_status === 'uncertain' || !finite(pair.speed_px_s)) {
          layer.append(svgNode('rect',{x:x(pair.start_s),y:bottom-6,width:Math.max(1,x(pair.end_s)-x(pair.start_s)),height:6,fill:'#617084',opacity:.8})); continue;
        }
        const yy=y(pair.speed_px_s);
        layer.append(svgNode('line',{x1:x(pair.start_s),x2:x(pair.end_s),y1:yy,y2:yy,stroke:COLORS.motion,'stroke-width':2.5,opacity:pair.status==='low_motion'?.55:1}));
      }
    } else for (const item of series) {
      let path='',drawing=false,previous=-Infinity;
      item.times.forEach((time,index) => {
        const value=item.values[index];
        if (!finite(value)) {drawing=false;return;}
        if (!drawing || time-previous>.15) path+=`M${x(time).toFixed(2)},${y(value).toFixed(2)}`;
        else path+=`H${x(time).toFixed(2)}V${y(value).toFixed(2)}`;
        drawing=true;previous=time;
      });
      layer.append(svgNode('path',{d:path,fill:'none',stroke:item.color,'stroke-width':item.dash?2.6:1.8,...(item.dash?{'stroke-dasharray':'7 4'}:{})}));
    }
    const marks=draft ? [...Object.entries(draft.anchors).map(([key,time])=>({time,label:ANCHORS.find(v=>v[0]===key)?.[1]})),...draft.custom_events.map(v=>({time:v.time_s,label:v.name}))] : [];
    for (const mark of marks) if (finite(mark.time)) layer.append(svgNode('line',{x1:x(mark.time),x2:x(mark.time),y1:top,y2:bottom,stroke:'#8adbbe',opacity:.8,'stroke-dasharray':'3 4'}));
    const cursor=svgNode('line',{x1:x(currentTime()),x2:x(currentTime()),y1:top-2,y2:bottom,stroke:'#eef6ff','stroke-width':1.25});svg.append(cursor);
    chartMeta.push({svg,cursor,x,top,bottom,left,right});
  }
  function xAxis(svg, y) {
    const left=62,right=840;
    for (let i=0;i<=5;i++) {
      const time=activeRange[0]+(activeRange[1]-activeRange[0])*i/5;
      svg.append(svgNode('text',{x:left+(right-left)*i/5,y,'text-anchor':'middle',fill:'#9db3c5','font-size':11},`${fmt(time,1)}s`));
    }
  }
  function drawCharts() {
    if (!data) return;
    activeRange=getRange();chartMeta=[];
    const base=$('base-chart');base.replaceChildren();
    const linear=[numericSeries('base',0,COLORS.vx,'vx'),numericSeries('base',1,COLORS.vy,'vy')];
    const rotation=[numericSeries('base',2,COLORS.wz,'wz')];
    if(data.relabel_candidate){const p=data.relabel_candidate;const colors=['#ff94d2','#ffe17d','#fbaf80'];for(let c=0;c<3;c++){const item={label:`候选${c}`,color:colors[c],times:p.time_s,values:p.base_v.map(v=>v[c]),dash:true};(c===2?rotation:linear).push(item);}}
    let [lo,hi]=limits(linear,.1);plotTrack(base,{top:30,bottom:91,unit:'vx / vy · m/s',series:linear,lo,hi});
    [lo,hi]=limits(rotation,.02);plotTrack(base,{top:120,bottom:155,unit:'wz · rad/s',series:rotation,lo,hi});
    let motionMax=1;
    for (const pair of data.motion_pairs || []) if (pair.end_s>=activeRange[0] && pair.start_s<=activeRange[1] && pair.status!=='uncertain' && pair.evidence_status!=='uncertain' && finite(pair.speed_px_s)) motionMax=Math.max(motionMax,pair.speed_px_s);
    plotTrack(base,{top:189,bottom:239,unit:'背景 · px/s',series:[],lo:0,hi:motionMax*1.08,motion:true});
    xAxis(base,259);
    for (const window of data.label_windows || []) {
      const mid=(window.start_s+window.end_s)/2;
      if (finite(mid) && mid>=activeRange[0] && mid<=activeRange[1]) base.append(svgNode('text',{x:62+(mid-activeRange[0])/(activeRange[1]-activeRange[0])*778,y:15,'text-anchor':'middle',fill:'#93afc8','font-size':10},window.label || '原移动窗口'));
    }
    drawArmChart();updateCursors();
  }
  function drawArmChart() {
    if (!data) return;
    const svg=$('arm-chart');svg.replaceChildren();chartMeta=chartMeta.filter(v=>v.svg!==svg);
    const jointMode=$('signal-mode').value==='joint';$('joint-label').classList.toggle('hidden',!jointMode);
    let series,unit;
    if (jointMode) {
      const joint=Number($('joint-select').value);
      series=[numericSeries('lead',joint,COLORS.lead,'lead 目标'),numericSeries('hal',joint,COLORS.hal,'HAL 反馈')];unit='关节角 · °';
    } else {
      series=[numericSeries('gripper_command',0,'#7bc7ff','左夹爪命令'),numericSeries('gripper_feedback',0,'#ffbd70','左夹爪反馈'),numericSeries('gripper_command',1,'#7bdfbc','右夹爪命令'),numericSeries('gripper_feedback',1,'#cbb4ff','右夹爪反馈')];unit='夹爪 · 0–100';
    }
    const [lo,hi]=jointMode?limits(series,2):[-5,105];
    plotTrack(svg,{top:30,bottom:138,unit,series,lo,hi});xAxis(svg,158);
    const legend=$('arm-legend');legend.replaceChildren();
    series.forEach(item => {const span=node('span',{},`━━ ${item.label}`);span.style.color=item.color;legend.append(span);});
    if (!jointMode) legend.append(node('span',{},'0 张开 / 100 闭合'));
    updateCursors();updateReadouts();
  }
  function updateCursors() {
    const time=currentTime();
    for (const chart of chartMeta) {
      const x=chart.x(time);chart.cursor.setAttribute('x1',x);chart.cursor.setAttribute('x2',x);
      chart.cursor.style.display=x<chart.left || x>chart.right?'none':'';
    }
  }
  function updateReadouts() {
    if (!data) return;
    const time=currentTime(),index=lastIndex(data.time_s,time);
    ['vx','vy','wz'].forEach((key,col) => text(`${key}-value`,fmt(valueAt('base',col,index))));
    if(data.relabel_candidate){const p=data.relabel_candidate,v=p.base_v[index];const status=p.timing_reviewed[index]?'时刻已复核，速度为弱估计':'自动建议，尚未复核';text('candidate-readout',v&&v.every(finite)?`重标候选：vx ${fmt(v[0])} m/s · vy ${fmt(v[1])} m/s · wz ${fmt(v[2])} rad/s（${status}）`:'重标候选：此时段未知或位于排除的尾段');}
    const pair=(data.motion_pairs || []).find(p=>time>=p.start_s && time<p.end_s);
    const uncertain=pair?.status==='uncertain'||pair?.evidence_status==='uncertain';
    const state=uncertain?'不确定':pair?.status==='motion'?'检测到运动':pair?.status==='low_motion'?'低像素运动':pair?'不确定':'无对应采样';
    text('motion-status',`背景：${state}${!uncertain&&finite(pair?.speed_px_s)?` · ${fmt(pair.speed_px_s,1)} px/s`:''}`);
    if ($('signal-mode').value==='joint') {
      const joint=Number($('joint-select').value);text('joint-readout',`lead ${fmt(valueAt('lead',joint,index),2)}° · HAL ${fmt(valueAt('hal',joint,index),2)}°`);
    } else text('joint-readout',`左 ${fmt(valueAt('gripper_command',0,index),0)} / ${fmt(valueAt('gripper_feedback',0,index),0)} · 右 ${fmt(valueAt('gripper_command',1,index),0)} / ${fmt(valueAt('gripper_feedback',1,index),0)}（命令 / 反馈）`);
  }
  function updateTime() {
    text('current-time',fmt(currentTime()));
    if (data && $('chart-window').value==='near' && Math.abs(currentTime()-lastNearRedraw)>.4) {lastNearRedraw=currentTime();drawCharts();}
    else updateCursors();
    updateReadouts();
    if (focusEnd!==null && currentTime()>=focusEnd) {video.pause();focusEnd=null;text('play-status','已到所选疑点的上下文末尾；可继续播放完整录制。');}
  }
  function startFrameLoop() {
    if (frameRequest!==null) cancelAnimationFrame(frameRequest);
    let last=0;
    const tick=stamp => {if(stamp-last>50){updateTime();last=stamp;}if(!video.paused&&!video.ended)frameRequest=requestAnimationFrame(tick);else frameRequest=null;};
    frameRequest=requestAnimationFrame(tick);
  }

  function draftKey() { return `f1-base-review-v1:${selected.episode}:${data.source_origin_ns}`; }
  function loadDraft() {
    draft={anchors:Object.fromEntries(ANCHORS.map(([key])=>[key,null])),custom_events:[],notes:'',updated_at:null};
    try {
      const raw=localStorage.getItem(draftKey());
      if (raw) {
        const saved=JSON.parse(raw);
        if (saved.episode===selected.episode && saved.source_origin_ns===data.source_origin_ns) {
          for (const [key] of ANCHORS) if (finite(saved.anchors?.[key]) && saved.anchors[key]>=0 && saved.anchors[key]<=duration()) draft.anchors[key]=saved.anchors[key];
          draft.custom_events=Array.isArray(saved.custom_events)?saved.custom_events.filter(item=>typeof item.name==='string').map(item=>({name:item.name,time_s:finite(item.time_s)&&item.time_s>=0&&item.time_s<=duration()?item.time_s:null})):[];
          draft.notes=typeof saved.notes==='string'?saved.notes:'';draft.updated_at=saved.updated_at||null;
        }
      }
      text('draft-status',draft.updated_at?'已恢复草稿':'本地草稿');
    } catch (_) {text('draft-status','浏览器不能保存草稿');}
    $('review-notes').value=draft.notes;
  }
  function feedback() {
    return {
      schema:'f1_base_manual_anchors_v1', review_manifest_schema:manifest.schema, review_manifest_created_at:manifest.created_at,
      episode:selected.episode, source_origin_ns:data.source_origin_ns, source_duration_s:duration(),
      time_reference:'seconds_since_original_mcap_log_start', annotation_status:'human_candidate_not_training_label',
      anchors:{...draft.anchors}, custom_events:draft.custom_events.map(item=>({...item})), notes:draft.notes,
      updated_at:draft.updated_at, provenance:data.provenance || {},
      limitations:'人工阶段候选；仅标时间不等于已恢复速度、方向或六段轨迹；尚未验收。'
    };
  }
  function saveDraft() {
    if (!draft || !data) return;
    draft.updated_at=new Date().toISOString();
    try {localStorage.setItem(draftKey(),JSON.stringify(feedback()));text('draft-status','已保存到浏览器');}
    catch (_) {text('draft-status','未能保存，请下载');}
    $('feedback-output').value=JSON.stringify(feedback(),null,2);
  }
  function markerRow(label, currentValue, onChange, options={}) {
    const row=node('div',{class:`anchor-row${options.custom?' custom-row':''}`});
    if (options.custom) {
      const name=node('input',{type:'text',class:'custom-name',placeholder:'例如：去程后退停止 / 右移开始','aria-label':'自定义事件名称'});name.value=label;
      name.addEventListener('input',()=>{options.onName(name.value);saveDraft();});row.append(name);
    }
    const inputId=options.id || `custom-time-${options.index}`;
    const fieldLabel=node('label',{for:inputId},options.custom?'事件时间':label);
    if (options.help) fieldLabel.title=options.help;
    row.append(fieldLabel);
    const input=node('input',{id:inputId,type:'number',min:'0',max:String(duration()),step:'0.001',inputmode:'decimal','aria-label':`${label || '自定义事件'}的原录制秒`});
    if (options.help) input.title=options.help;
    input.value=finite(currentValue)?currentValue.toFixed(3):'';
    input.addEventListener('change',()=>{
      const value=input.value===''?null:Number(input.value);
      if (value!==null && (!finite(value)||value<0||value>duration())) {input.setCustomValidity(`请输入 0 到 ${duration().toFixed(3)} 秒`);input.reportValidity();return;}
      input.setCustomValidity('');onChange(value);saveDraft();drawCharts();
    });
    input.addEventListener('input',()=>input.setCustomValidity(''));
    const now=node('button',{type:'button','data-action':'mark-current','data-anchor':options.id||`custom-${options.index}`,title:`将${label || '事件'}设为当前画面时刻`},'此刻');
    now.addEventListener('click',()=>{if(!hasViewedFrame || pendingSeek!==null || video.readyState<2 || video.seeking){text('export-status','请先播放或定位并等待实际画面载入；封面预览不能用于“此刻”标记。');return;}const value=Math.min(Math.floor(duration()*1000)/1000,Number(currentTime().toFixed(3)));input.value=value.toFixed(3);input.setCustomValidity('');onChange(value);saveDraft();drawCharts();});
    const clear=node('button',{type:'button',class:'clear-anchor','data-action':'clear-marker','aria-label':options.custom?'删除此自定义事件':`清空${label}`},'×');
    clear.addEventListener('click',()=>{if(options.custom){options.onRemove();buildAnchors();}else{input.value='';input.setCustomValidity('');onChange(null);}saveDraft();drawCharts();});
    row.append(input,now,clear);return row;
  }
  function buildAnchors() {
    const fields=$('anchor-fields');fields.replaceChildren();
    ANCHORS.forEach(([key,label])=>fields.append(markerRow(label,draft.anchors[key],value=>{draft.anchors[key]=value;},{id:`anchor-${key}`,help:ANCHOR_HELP[key]})));
    const custom=$('custom-events');custom.replaceChildren();
    draft.custom_events.forEach((event,index)=>custom.append(markerRow(event.name,event.time_s,value=>{event.time_s=value;},{custom:true,index,onName:name=>{event.name=name;},onRemove:()=>draft.custom_events.splice(index,1)})));
  }
  function downloadFeedback() {
    if (!draft || !validMarkerInputs()) return;
    saveDraft();const blob=new Blob([JSON.stringify(feedback(),null,2)+'\n'],{type:'application/json;charset=utf-8'});
    const url=URL.createObjectURL(blob),link=node('a',{href:url,download:`${selected.id}_base_anchors.json`});
    document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
    text('export-status','已生成当前录制的反馈 JSON。');
  }
  function validMarkerInputs() {
    for (const input of $('markers-panel').querySelectorAll('input[type="number"]')) {
      if (!input.checkValidity()) {input.reportValidity();text('export-status','请先修正标记时间，再导出。');return false;}
    }
    return true;
  }

  for(let index=0;index<14;index++) $('joint-select').append(node('option',{value:index},`${index<7?'左臂':'右臂'} J${index%7+1}`));
  $('joint-select').value='4';
  $('play-pause').addEventListener('click',togglePlay);
  $('back-one').addEventListener('click',()=>{focusEnd=null;seekTo(currentTime()-1);});
  $('forward-one').addEventListener('click',()=>{focusEnd=null;seekTo(currentTime()+1);});
  $('playback-speed').addEventListener('change',()=>{video.playbackRate=Number($('playback-speed').value);});
  $('chart-window').addEventListener('change',drawCharts);
  $('signal-mode').addEventListener('change',drawArmChart);
  $('joint-select').addEventListener('change',drawArmChart);
  $('review-notes').addEventListener('input',()=>{if(draft){draft.notes=$('review-notes').value;saveDraft();}});
  $('add-custom').addEventListener('click',()=>{if(!draft)return;draft.custom_events.push({name:'',time_s:null});saveDraft();buildAnchors();$('custom-events').lastElementChild.querySelector('input').focus();});
  $('copy-feedback').addEventListener('click',async()=>{
    if(!draft||!validMarkerInputs())return;saveDraft();const output=JSON.stringify(feedback(),null,2);$('feedback-output').value=output;
    try {await navigator.clipboard.writeText(output);text('export-status','已复制当前录制反馈，粘贴到对话即可。');}
    catch (_) {$('copy-fallback').classList.remove('hidden');$('copy-fallback').open=true;$('feedback-output').focus();$('feedback-output').select();text('export-status','浏览器限制自动复制；已选中下方 JSON，可按 Ctrl+C。');}
  });
  $('download-feedback').addEventListener('click',downloadFeedback);
  for(const svg of [$('base-chart'),$('arm-chart')]) svg.addEventListener('click',event=>{
    if(!data||!activeRange)return;const rect=svg.getBoundingClientRect(),px=(event.clientX-rect.left)/rect.width*860;
    focusEnd=null;seekTo(activeRange[0]+Math.max(0,Math.min(1,(px-62)/778))*(activeRange[1]-activeRange[0]));
  });
  video.addEventListener('loadedmetadata',()=>{if(pendingSeek!==null){const target=pendingSeek;pendingSeek=null;video.currentTime=Math.min(target,Math.max(0,video.duration-.001));}updateTime();});
  video.addEventListener('error',()=>{if(video.getAttribute('src'))showVideoError('视频加载失败。请检查端口转发后刷新；也可以尝试“下载视频”。');});
  video.addEventListener('play',()=>{text('play-pause','暂停');text('play-status',focusEnd===null?'正在播放完整录制。':`播放疑点及上下文，至 ${fmt(focusEnd,2)} 秒暂停。`);startFrameLoop();});
  video.addEventListener('pause',()=>{text('play-pause','播放');updateTime();});
  for(const event of ['playing','seeked'])video.addEventListener(event,()=>{if(video.readyState>=2 && !video.seeking)hasViewedFrame=true;});
  for(const event of ['timeupdate','seeked','ended'])video.addEventListener(event,updateTime);
  document.addEventListener('keydown',event=>{
    if(event.target.closest('input,textarea,select,button,a,[contenteditable="true"]')||event.ctrlKey||event.metaKey||event.altKey||!data)return;
    if(event.code==='Space'){event.preventDefault();togglePlay();}
    else if(event.code==='ArrowLeft'||event.code==='ArrowRight'){event.preventDefault();focusEnd=null;seekTo(currentTime()+(event.code==='ArrowRight'?1:-1));}
  });

  async function init() {
    try {
      const response=await fetch('manifest.json',{cache:'no-cache'});
      if(!response.ok)throw new Error(`清单请求失败（HTTP ${response.status}）`);
      manifest=await response.json();
      if(manifest.schema!=='f1_base_review_v1'||!Array.isArray(manifest.episodes)||!manifest.episodes.length)throw new Error('审核清单为空或版本不匹配');
      text('sample-notice',manifest.notice || '自动疑点是复核线索；请结合固定背景、手臂和夹爪，标记实际阶段。');
      try{const response=await fetch('relabel_preview_manifest.json',{cache:'no-cache'});if(response.ok)relabelPreviews=(await response.json()).episodes||[];}catch(_){}
      const requested=new URLSearchParams(location.search).get('episode');
      buildList();await selectEpisode(manifest.episodes.find(e=>e.id===requested||e.episode===requested)||manifest.episodes[0]);
    } catch(error){showError(`页面无法加载：${error.message}。请确认通过端口转发打开 HTTP 页面，然后刷新。`);text('load-status','审核清单未载入。');text('episode-count','未载入');}
  }
  init();
})();

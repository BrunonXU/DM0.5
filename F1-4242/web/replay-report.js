// Presentation only. The server owns comparison metrics and execution status.
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=(v,d=3)=>Number.isFinite(v)?v.toFixed(d):'—';
const name=n=>n.startsWith('arm_')?`${n.startsWith('arm_l')?'左臂':'右臂'} J${n.split('_j')[1]}`:n.startsWith('left')?'左夹爪':'右夹爪';
const unit=c=>c.unit==='deg'?'°':'开度百分点';

export function renderReplayReport(root,report){
 root._replayReportResize?.disconnect();
 const c=report.comparison||{},channels=c.channels||[],arms=channels.filter(x=>x.unit==='deg');
 const worst=arms.filter(x=>x.metrics).reduce((a,b)=>!a||b.metrics.max_abs>a.metrics.max_abs?b:a,null);
 const both=c.arms_summary?.both,complete=report.result?.status==='succeeded';
 const scale=Math.max(0,...arms.map(x=>x.metrics?.mean_abs||0));
 const groups=['left','right'].map((side,index)=>{
  const rows=channels.map((x,i)=>({x,i})).filter(({x})=>x.name.startsWith(index?'arm_r':'arm_l'));
  if(!rows.length)return '';
  const summary=c.arms_summary?.[side];
  return `<section class="rr-arm"><header><h5>${index?'右臂':'左臂'}</h5><span>平均 <b>${fmt(summary?.metrics?.mean_abs)}°</b></span></header>
   <div class="rr-column-labels"><span>关节</span><span>平均偏差 / °</span><span>最大 / °</span></div>
   ${rows.map(({x,i})=>`<button type="button" class="rr-joint" data-rr-joint="${i}" aria-label="查看${esc(name(x.name))}轨迹" aria-pressed="false"><span>J${esc(x.name.split('_j')[1])}</span><span class="rr-bar-value"><i aria-hidden="true" style="width:${scale?Math.max(0,x.metrics?.mean_abs||0)/scale*100:0}%"></i><b>${fmt(x.metrics?.mean_abs)}</b></span><span>${fmt(x.metrics?.max_abs)}</span></button>`).join('')}
  </section>`;
 }).join('');
 const grips=channels.map((x,i)=>({x,i})).filter(({x})=>x.unit!=='deg');
 root.innerHTML=`<section class="replay-result replay-report">
  <header class="rr-heading"><div><h4>回放对比</h4><p>原始录制实测 · 本次回放实测</p></div><span class="rr-badge ${complete?'':'rr-partial'}">${complete?'整段已完成':'仅已执行部分'}</span></header>
  <div class="rr-kpis">
   <div class="rr-kpi rr-primary"><span>${both?.complete?'双臂平均偏差':'已比较关节平均偏差'}</span><strong>${fmt(both?.metrics?.mean_abs)}<small>°</small></strong><p>平均绝对偏差 · 越小越接近</p></div>
   <div class="rr-kpi"><span>最大瞬时偏差</span><strong>${fmt(worst?.metrics?.max_abs)}<small>°</small></strong><p>${worst?`${esc(name(worst.name))} · ${fmt(worst.metrics.max_at_s,2)} 秒`:'暂无可比较数据'}</p></div>
   <div class="rr-kpi"><span>参与比较的关节</span><strong>${both?.joints_compared??'—'}<small>/ ${both?.joints_total??14}</small></strong><p>${both?.complete?'14 个手臂关节均有比较数据':'缺失数据不计入平均'}</p></div>
  </div>
  ${groups?`<div class="rr-section-heading"><h5>关节偏差</h5><span>点击关节查看曲线 · 两臂共用比例尺</span></div><div class="rr-arms">${groups}</div>`:'<p class="rr-empty">这份记录没有逐关节对比数据。</p>'}
  ${grips.length?`<div class="rr-grippers">${grips.map(({x,i})=>`<button type="button" data-rr-joint="${i}" aria-pressed="false"><span>${esc(name(x.name))}</span><b>${fmt(x.metrics?.mean_abs)}</b><small>平均偏差 / 开度百分点</small></button>`).join('')}</div>`:''}
  ${channels.length?`<section class="rr-inspector"><div class="rr-inspector-heading"><label>查看 <select data-r-channel aria-label="选择对比关节">${channels.map((x,i)=>`<option value="${i}">${esc(name(x.name))}</option>`).join('')}</select></label><div class="rr-tabs" aria-label="曲线类型"><button type="button" data-rr-mode="error" aria-pressed="true">偏差曲线</button><button type="button" data-rr-mode="trace" aria-pressed="false">轨迹叠加</button></div></div><div data-r-chart></div></section>`:''}
  <details class="rr-details"><summary>完整数据与统计说明</summary>
   ${channels.length?`<div class="rr-table-wrap"><table data-r-metrics><thead><tr><th>部位</th><th>平均绝对偏差</th><th>均方根偏差</th><th>最大偏差</th><th>有效 / 采样点</th></tr></thead><tbody>${channels.map(x=>`<tr><th>${esc(name(x.name))}</th><td>${fmt(x.metrics?.mean_abs)} ${unit(x)}</td><td>${fmt(x.metrics?.rms)} ${unit(x)}</td><td>${fmt(x.metrics?.max_abs)} ${unit(x)}</td><td>${x.matched??0} / ${x.replay_samples??0}</td></tr>`).join('')}</tbody></table></div>`:''}
   <p>按相对时间对齐两次实测，双臂平均对有至少 2 个比较点的关节等权计算。夹爪单列；缺失值显示 —，不按零计算。曲线使用报告中的绘图采样，统计使用全部有效比较点。</p>
   <p>平均绝对偏差表示通常相差多少；均方根偏差更突出较大的误差；最大偏差表示最偏离的一刻。这里展示偏差，不自动判定硬件是否合格。${complete?'':'本次未完整执行，所有结果仅代表已执行部分。'}</p>
   <p>原始反馈只在允许的时间间隙内插值，不外推、不用命令填补实测。接收时间不等于电机采样时间。</p>
   ${grips.map(({x})=>{const g=x.crossings_50_percent;return g?`<p>${esc(name(x.name))}跨越 50% 开度：原始 ${g.original_count} 次，本次 ${g.replay_count} 次${g.comparable&&g.deltas_s?.length?'；时差 '+g.deltas_s.map(v=>fmt(v)).join(' / ')+' 秒':''}</p>`:'';}).join('')}
   ${report.result?.dispatch_timing?`<p>下发时间误差：平均 ${fmt(report.result.dispatch_timing.mean_lateness_ms)} ms，最大 ${fmt(report.result.dispatch_timing.max_lateness_ms)} ms。</p>`:''}
   ${report.result?.playback_timing?.total_shift_s>0?`<p>轨迹顺延 ${fmt(report.result.playback_timing.total_shift_s)} 秒；全部目标按原顺序保留。对比仍按真实经过时间计算，顺延造成的时间差也计入偏差。</p>`:''}
   ${report.result?.final_target_reached===false?'<p>结束时仍有目标偏差，已记录。</p>':''}
  </details>
 </section>`;
 if(!channels.length)return;
 const select=root.querySelector('[data-r-channel]');let mode='error';
 select.value=String(Math.max(0,channels.indexOf(worst)));
 const draw=()=>{
  root.querySelectorAll('[data-rr-joint]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.rrJoint===select.value)));
  root.querySelectorAll('[data-rr-mode]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.rrMode===mode)));
  drawChart(root.querySelector('[data-r-chart]'),channels[Number(select.value)],mode);
 };
 select.onchange=draw;
 root.querySelectorAll('[data-rr-joint]').forEach(b=>b.onclick=()=>{select.value=b.dataset.rrJoint;draw();});
 root.querySelectorAll('[data-rr-mode]').forEach(b=>b.onclick=()=>{mode=b.dataset.rrMode;draw();});
 draw();
 let previousWidth=root.clientWidth;
 const observer=new ResizeObserver(()=>{if(!root.isConnected){observer.disconnect();return;}const width=root.clientWidth;if(width>0&&Math.abs(width-previousWidth)>1){previousWidth=width;draw();}});
 observer.observe(root);root._replayReportResize=observer;
}

function drawChart(root,channel,mode){
 const points=(channel.points||[]).filter(p=>Number.isFinite(p[0]));
 const valid=points.filter(p=>Number.isFinite(p[1])&&Number.isFinite(p[2]));
 if(!valid.length){root.innerHTML='<p class="rr-empty">该关节没有可比较的实测反馈。</p>';return;}
 const error=mode==='error',u=unit(channel),first=points[0][0],last=points[points.length-1][0],span=last-first||1;
 const values=error?valid.map(p=>p[2]-p[1]):points.flatMap(p=>p.slice(1).filter(Number.isFinite));
 let lo=Math.min(...values),hi=Math.max(...values);
 if(error){const extent=Math.max(Math.abs(lo),Math.abs(hi),.001)*1.15;lo=-extent;hi=extent;}
 else {const pad=Math.max((hi-lo)*.12,.001);lo-=pad;hi+=pad;}
 const width=Math.max(280,root.clientWidth||800),right=width-18,plotWidth=right-60;
 const X=t=>60+(t-first)/span*plotWidth,Y=v=>24+(hi-v)/(hi-lo)*160;
 const path=fn=>{let pen=false;return points.map(p=>{const v=fn(p);if(!Number.isFinite(v)){pen=false;return '';}const cmd=pen?'L':'M';pen=true;return `${cmd}${X(p[0]).toFixed(2)},${Y(v).toFixed(2)}`;}).join(' ');};
 const delta=p=>Number.isFinite(p[1])&&Number.isFinite(p[2])?p[2]-p[1]:null;
 const grid=Array.from({length:5},(_,i)=>{const v=lo+(hi-lo)*i/4,y=Y(v);return `<line x1="60" x2="${right}" y1="${y}" y2="${y}" class="rr-grid"/><text x="50" y="${y+4}" text-anchor="end">${fmt(v)}</text>`;}).join('');
 const ticks=Array.from({length:5},(_,i)=>{const t=first+span*i/4;return `<text x="${X(t)}" y="207" text-anchor="middle">${fmt(t,1)}s</text>`;}).join('');
 root.innerHTML=`<div class="rr-chart-summary"><span>平均 <b>${fmt(channel.metrics?.mean_abs)} ${u}</b></span><span>最大 <b>${fmt(channel.metrics?.max_abs)} ${u}</b></span><span>均方根 <b>${fmt(channel.metrics?.rms)} ${u}</b></span></div>
  <div class="rr-legend">${error?`<span><i class="rr-error-key"></i>本次 − 原始 / ${u}</span><small>越接近 0，越一致</small>`:`<span><i class="rr-original-key"></i>原始实测</span><span><i class="rr-current-key"></i>本次实测</span><small>${u}</small>`}</div>
  <svg class="rr-chart" viewBox="0 0 ${width} 220" role="img" aria-label="${esc(name(channel.name))}${error?'偏差曲线':'实测轨迹对比'}">${grid}${ticks}
   ${error?`<line x1="60" x2="${right}" y1="${Y(0)}" y2="${Y(0)}" class="rr-zero"/><path d="${path(delta)}" class="rr-error-line"/>`:`<path d="${path(p=>p[1])}" class="rr-original-line"/><path d="${path(p=>p[2])}" class="rr-current-line"/>`}
   <line data-rr-cursor x1="60" x2="60" y1="24" y2="184" class="rr-cursor"/>
  </svg><input class="rr-scrubber" data-rr-scrubber type="range" min="0" max="${points.length-1}" value="0" step="1" aria-label="查看曲线上各时刻的实测值"><div class="rr-readout" data-rr-readout></div>`;
 const svg=root.querySelector('svg'),slider=root.querySelector('[data-rr-scrubber]'),readout=root.querySelector('[data-rr-readout]');
 const inspect=i=>{const p=points[i],d=delta(p),x=X(p[0]);slider.value=String(i);root.querySelector('[data-rr-cursor]').setAttribute('x1',x);root.querySelector('[data-rr-cursor]').setAttribute('x2',x);readout.innerHTML=`<b>${fmt(p[0],2)} 秒</b><span>原始 ${fmt(p[1])}${u}</span><span>本次 ${fmt(p[2])}${u}</span><span>偏差 <b>${Number.isFinite(d)&&d>0?'+':''}${fmt(d)}${u}</b></span>`;slider.setAttribute('aria-valuetext',`${fmt(p[0],2)} 秒，偏差 ${fmt(d)} ${u}`);};
 slider.oninput=()=>inspect(Number(slider.value));
 svg.onpointermove=e=>{const box=svg.getBoundingClientRect(),t=first+Math.min(1,Math.max(0,((e.clientX-box.left)/box.width*width-60)/plotWidth))*span;let l=0,r=points.length-1;while(l<r){const m=(l+r)>>1;if(points[m][0]<t)l=m+1;else r=m;}if(l&&Math.abs(points[l-1][0]-t)<Math.abs(points[l][0]-t))l--;inspect(l);};
 inspect(points.indexOf(valid[0]));
}

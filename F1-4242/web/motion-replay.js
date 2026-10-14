// Read-only preparation; every motion requires a separate onsite confirmation.
const drafts=new Map();
const running=a=>['accepted','queued','running','stopping'].includes(a?.status);
const escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number=n=>Number.isFinite(n)?n.toFixed(3):'—';
const label=n=>n.startsWith('arm_')?`${n.startsWith('arm_l')?'左臂':'右臂'} J${n.split('_j')[1]}`:n.startsWith('left')?'左夹爪':'右夹爪';
const eventName=n=>({home_started:'Home 开始',home_stopped:'Home 停止',home_stage_completed_feedback:'Home 阶段到位',home_completed_feedback:'Home 到位',demonstration_stopped:'录制结束'})[n]||n;
export const motionReplayHTML=e=>e.source==='hil_demonstration'?'<section class="motion-replay" data-motion-replay hidden></section>':'';

export class MotionReplay {
 constructor(root,episode,hooks){
  Object.assign(this,{root,episode,hooks,alive:true,generation:0});
  this.draft=drafts.get(episode.id)||{start:'0',end:'',open:false};drafts.set(episode.id,this.draft);
  if(this.draft.open)this.open();
 }
 q(s){return this.root.querySelector(s);}
 open(){
  if(this.draft.open&&this.q('[data-r-start-task]')){this.root.hidden=false;this.hooks.expanded?.(true);return;}
  this.draft.open=true;this.root.hidden=false;this.hooks.expanded?.(true);
  this.root.innerHTML=`<header class="replay-heading"><div><h3>实机 Replay</h3><span class="replay-mode">原速 · 单次 · 双臂与夹爪</span></div><button class="button small" data-r-close>收起</button></header>
   <div class="replay-console"><div class="replay-overview"><div class="replay-readiness" data-r-review aria-live="polite"></div><dl class="replay-metrics"><div><dt>回放时长</dt><dd><span data-r-duration>—</span><small>秒</small></dd></div><div><dt>原始命令</dt><dd><span data-r-count>—</span><small>条</small></dd></div></dl></div>
    <div class="replay-buttons"><button class="button primary replay-start" data-r-start-task><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m6 3 11 7-11 7Z" fill="currentColor"/></svg>开始回放</button><button class="button" data-r-align disabled>回到录制起点</button><button class="button danger replay-stop" data-r-stop disabled><svg viewBox="0 0 20 20" aria-hidden="true"><rect x="4" y="4" width="12" height="12" rx="1" fill="currentColor"/></svg>停止</button></div>
   </div><div class="replay-execution" data-r-status aria-live="polite"></div><div data-r-comparison></div>
   <div class="replay-settings"><details class="replay-disclosure" data-r-more><summary>回放区间 <span data-r-range-label>整条录制</span></summary><div class="replay-range"><label>开始 / 秒<input data-r-start type="number" min="0" step="any" value="${escape(this.draft.start)}"></label><label>结束 / 秒<input data-r-end type="number" min="0" step="any" placeholder="录制末尾" value="${escape(this.draft.end)}"></label><button class="button" data-r-check>重新检查</button></div></details>
    <details class="replay-disclosure" data-r-diagnostics><summary>检查与诊断</summary><div data-r-details></div><button class="button small" data-r-release hidden>释放控制权</button><p class="section-note">检查只读；回到起点与回放分别确认。软件停止不等于硬件急停，失联后不会自动续跑。</p></details></div>`;
  this.q('[data-r-close]').onclick=()=>{this.draft.open=false;this.root.hidden=true;this.hooks.expanded?.(false);};
  for(const name of ['start','end'])this.q('[data-r-'+name+']').oninput=e=>{this.draft[name]=e.target.value;this.review=null;this.generation++;this.q('[data-r-review]').textContent='区间已修改 · 开始前重新检查';this.q('[data-r-duration]').textContent='—';this.q('[data-r-count]').textContent='—';this.q('[data-r-range-label]').textContent='自定义区间';this.update();};
  this.q('[data-r-check]').onclick=()=>this.check();
  this.q('[data-r-align]').onclick=()=>this.begin(true);
  this.q('[data-r-start-task]').onclick=()=>this.begin(false);
  this.q('[data-r-release]').onclick=()=>this.hooks.run(this.hooks.release);
  this.q('[data-r-stop]').onclick=()=>this.hooks.run(this.hooks.stop);
  this.update();this.check();
 }
 async check(){
  const generation=++this.generation;this.review=null;this.busy=true;this.update();
  const holder=this.q('[data-r-review]');holder.textContent='正在检查数据与起点…';
  try{
   const params=new URLSearchParams({episode_id:this.episode.id,start_s:this.draft.start||'0'});
   if(this.draft.end!=='')params.set('end_s',this.draft.end);
   const {review}=await this.hooks.api('/api/replay/review?'+params,undefined,{timeout:30000});
   if(!this.alive||generation!==this.generation)return null;
   this.q('.replay-mode').textContent=(this.draft.end!==''||Number(this.draft.start)>0?'所选区间':'整条')+' · 原速 · 单次';
   this.review=review;this.expires=Date.now()+(review.valid_for_s||0)*1000;
   holder.innerHTML=review.executable?'<span class="replay-state is-ready" data-r-ready>起点已就绪</span>':`<span class="replay-state is-blocked">${review.can_align?'需要对齐起点':'暂不可回放'}</span><p class="replay-blockers">${(review.issues||[]).map(escape).join('<br>')}</p>`;
   this.q('[data-r-duration]').textContent=number(review.duration_s);
   this.q('[data-r-count]').textContent=Number.isFinite(review.command_count)?review.command_count.toLocaleString():'—';
   this.q('[data-r-range-label]').textContent=Number(this.draft.start||0)===0&&this.draft.end===''?'整条录制':`${number(review.start_s)}–${number(review.end_s)} 秒`;
   this.q('[data-r-details]').innerHTML=`<p class="replay-detail-range">${number(review.start_s)}–${number(review.end_s)} 秒 · ${review.command_count} 条原始命令</p><p class="section-note">时间从首条命令算起；两次实测按各自反馈时间对齐，图像时间可能略有偏移。</p>${review.events?.length?`<details class="replay-events"><summary>历史事件 <span>${review.events.length} 条</span></summary><ul>${review.events.map(e=>`<li><time>${number(e.time_s)}s</time><span>${escape(eventName(e.event))}${e.reason?' · '+escape(e.reason):''}</span></li>`).join('')}</ul></details>`:''}`;
   return review;
  }catch(error){if(this.alive&&generation===this.generation)holder.textContent=error.message;return null;}
  finally{if(this.alive&&generation===this.generation){this.busy=false;this.update();}}
 }
 update(){
  if(!this.alive||!this.draft.open)return;
  const state=this.hooks.state(),lease=this.hooks.lease(),action=state?.action,active=running(action),own=action?.replay?.episode_id===this.episode.id;
  const busy=this.busy||this.submitting||this.intent||active||this.hooks.executing();
  for(const s of ['[data-r-start]','[data-r-end]','[data-r-check]'])this.q(s).disabled=!!busy;
  this.q('[data-r-align]').disabled=!!busy||!this.review?.can_align;
  this.q('[data-r-start-task]').disabled=!!busy||!!(this.review&&!this.review.executable);
  this.q('[data-r-stop]').disabled=!lease||(!active&&!this.submitting);
  this.q('[data-r-release]').hidden=!lease;this.q('[data-r-release]').disabled=!!this.submitting;
  const readiness=this.q('[data-r-review]');readiness.hidden=!!(own&&active);
  if(own&&!this.review&&!busy)readiness.textContent='再次开始前会检查起点';
  if(this.review?.executable&&Date.now()>=this.expires&&this.q('[data-r-ready]')){this.q('[data-r-ready]').textContent='检查已过期 · 开始前自动复查';this.q('[data-r-ready]').classList.remove('is-ready');}
  const status=this.q('[data-r-status]');
  if(own){
   const align=action.replay.phase==='alignment',p=action.progress_detail||{},names={accepted:'已受理',queued:'等待执行',running:align?'正在回到起点':'正在回放',stopping:'正在停止',cancelled:'已停止',rejected:'未执行',failed:'执行中断',succeeded:align?'已回到起点，请点击开始回放':'回放执行完成'};
   status.innerHTML=`<div class="replay-progress-heading"><strong>${escape(names[action.status]||action.status)}</strong>${active?`<span>${number(p.elapsed_s)}<small> / ${number(p.estimated_total_s??this.review?.duration_s)} 秒</small></span>`:''}</div>${active?`<progress aria-label="${align?'起点对齐':'回放'}进度" max="1" value="${Math.max(0,Math.min(1,action.progress||0))}"></progress>`:''}${action.error?`<p class="notice warn">${escape(action.error)}</p>`:''}`;
   if(align&&!active&&this.alignedAction!==action.id){this.alignedAction=action.id;queueMicrotask(()=>{if(this.alive&&!this.busy)this.check();});}
   if(!align&&!active&&action.replay.report_id&&this.reportId!==action.replay.report_id){this.reportId=action.replay.report_id;this.loadReport(this.reportId);}
  }else status.textContent=active?'其他动作正在执行，请等待结束。':'';
 }
 async begin(align){
  if(this.intent||this.submitting||running(this.hooks.state()?.action))return;
  this.intent=true;this.update();
  try{
   if(!this.hooks.lease())await this.hooks.acquire();
   if(!this.alive||!this.hooks.lease())return;
   const review=await this.check();
   if(!review||!review[align?'can_align':'executable'])return;
   this.confirm(review,align);
  }finally{this.intent=false;this.update();}
 }
 confirm(review,align){
  if(this.dialog)return;
  const dialog=document.createElement('dialog');this.dialog=dialog;dialog.className='execute-dialog replay-confirm';
  const warnings=(review.events||[]).filter(e=>e.selected&&/failed|fault|disconnect/.test(e.event));
  dialog.innerHTML=`<form><h2>${align?'回到录制起点':'开始回放'}</h2><p>${escape(this.episode.name)}</p><p>${align?'双臂、头身和夹爪将按现有调姿路径回到录制起点，到位后等待你开始回放。':`${number(review.duration_s)} 秒 · 原速 · 单次`}</p>${align&&review.alignment_plan?`<p>预计 ${number(review.alignment_plan.estimated_total_s)} 秒</p>`:''}${align&&review.alignment_plan?.head_boundary_recovery?`<p class="notice warn">头部当前 ${number(review.alignment_plan.head_boundary_recovery.origin)}°，将先执行原生边界恢复到 ${number(review.alignment_plan.head_boundary_recovery.edge)}°。该首步速度不可控，确认即包含此次恢复。</p>`:''}${!align&&warnings.length?`<p class="notice warn">此区间有 ${warnings.length} 条历史异常，请确认仍要复现。</p>`:''}<p>请在现场确认运动路径与周围空间。确认后机器人将运动。</p><p data-r-error role="alert"></p><div class="dialog-actions"><button class="button" type="button" data-r-cancel>取消</button><button class="button primary" type="submit">${align?'确认回到起点':'确认开始'}</button></div></form>`;
  document.body.append(dialog);
  dialog.querySelector('[data-r-cancel]').onclick=()=>{if(this.submitting)this.hooks.run(async()=>{await this.hooks.stop();dialog.close();});else dialog.close();};
  dialog.addEventListener('close',()=>{dialog.remove();if(this.dialog===dialog)this.dialog=null;});
  dialog.addEventListener('cancel',e=>{if(this.submitting)e.preventDefault();});
  dialog.querySelector('form').onsubmit=async e=>{
   e.preventDefault();if(this.submitting)return;
   this.submitting=true;dialog.querySelector('[type=submit]').disabled=true;dialog.querySelector('[data-r-cancel]').textContent='停止请求';this.update();
   try{
    const {action}=await this.hooks.api('/api/replay/'+(align?'align':'start'),{review_id:review.review_id,lease_id:this.hooks.lease(),confirm:true,head_boundary_recovery_ack:!!(align&&review.alignment_plan?.head_boundary_recovery)},{timeout:30000});
    this.reportId=null;this.q('[data-r-comparison]').textContent='';this.hooks.accept(action);dialog.close();
   }catch(error){if(dialog.isConnected)dialog.querySelector('[data-r-error]').textContent=error.message+'；关闭后可重新开始。';}
   finally{this.submitting=false;this.review=null;dialog.querySelector('[data-r-cancel]').textContent='关闭';if(this.alive)this.update();await this.hooks.refresh();}
  };
  dialog.showModal();dialog.querySelector('[data-r-cancel]').focus();
 }
 async loadReport(id){
  const holder=this.q('[data-r-comparison]');holder.textContent='正在读取实测对比…';
  try{
   const {report}=await this.hooks.api('/api/replay/reports/'+id);
   if(!this.alive||this.reportId!==id)return;
   const c=report.comparison,t=report.result?.dispatch_timing;
   const metrics=(c?.channels||[]).filter(x=>x.metrics&&x.unit==='deg'),worst=metrics.reduce((a,b)=>!a||b.metrics.max_abs>a.metrics.max_abs?b:a,null);
   holder.innerHTML=`<section class="replay-result"><div class="replay-result-heading"><h4>两次实测对比</h4>${report.result?.status!=='succeeded'?'<span class="replay-state is-blocked">未完整执行</span>':''}</div>${worst?`<div class="replay-comparison-summary"><div><span>最大轨迹偏差</span><strong>${number(worst.metrics.max_abs)}<small>°</small></strong></div><p><b>${escape(label(worst.name))}</b><span>发生于 ${number(worst.metrics.max_at_s)} 秒</span></p></div>`:'<p>没有足够的两次实测反馈，无法比较。</p>'}<p class="section-note">${escape(c?.verdict||'此旧记录未包含两次实测对比。')}</p>${c?.available?`<details class="replay-disclosure"><summary>轨迹叠加与关节偏差</summary><label class="replay-channel-select">部位 <select data-r-channel>${c.channels.map((x,i)=>`<option value="${i}" ${x===worst?'selected':''}>${escape(label(x.name))}</option>`).join('')}</select></label><div data-r-chart></div><div class="table-scroll"><table><thead><tr><th>部位</th><th>平均绝对偏差</th><th>最大偏差</th><th>比较 / 采样点</th></tr></thead><tbody>${c.channels.map(x=>`<tr><td>${escape(label(x.name))}</td><td>${number(x.metrics?.mean_abs)} ${x.unit==='deg'?'°':'%'}</td><td>${number(x.metrics?.max_abs)} ${x.unit==='deg'?'°':'%'}</td><td>${x.matched}/${x.replay_samples}</td></tr>`).join('')}</tbody></table></div><p class="section-note">按反馈接收时间比较，最多50Hz采样；原始反馈间隙过大处不插值，未比较的点不计入误差。</p>${c.channels.filter(x=>x.crossings_50_percent).map(x=>{const g=x.crossings_50_percent;return `<p class="section-note">${escape(label(x.name))}越过50%开度：原始 ${g.original_count} 次，本次 ${g.replay_count} 次${g.comparable&&g.deltas_s.length?'；时差 '+g.deltas_s.map(number).join(' / ')+' 秒':''}</p>`;}).join('')}${t?`<p class="section-note">下发时间误差：平均 ${number(t.mean_lateness_ms)} ms，最大 ${number(t.max_lateness_ms)} ms</p>`:''}</details>`:t?`<details class="replay-disclosure"><summary>下发时间</summary><p class="section-note">平均误差 ${number(t.mean_lateness_ms)} ms · 最大误差 ${number(t.max_lateness_ms)} ms</p></details>`:''}</section>`;
   if(c?.available){const select=holder.querySelector('[data-r-channel]');const draw=()=>this.chart(holder.querySelector('[data-r-chart]'),c.channels[Number(select.value)]);select.onchange=draw;draw();}
  }catch(error){if(this.alive&&this.reportId===id)holder.textContent='对比记录暂不可用：'+error.message;}
 }
 chart(root,channel){
  const points=channel.points,values=points.flatMap(p=>p.slice(1).filter(Number.isFinite));if(!values.length){root.textContent='该部位没有可比较反馈。';return;}
  const lo=Math.min(...values),hi=Math.max(...values),last=points[points.length-1][0]||1,first=points[0][0],span=last-first||1;
  const x=t=>40+600*(t-first)/span,y=v=>190-160*(v-lo)/(hi-lo||1);
  const path=index=>{let pen=false;return points.map(p=>{if(!Number.isFinite(p[index])){pen=false;return '';}const cmd=pen?'L':'M';pen=true;return cmd+x(p[0]).toFixed(2)+','+y(p[index]).toFixed(2);}).join(' ');};
  root.innerHTML=`<div class="replay-chart-legend"><span><i></i>原始实测</span><span><i></i>本次实测</span><small>${escape(channel.unit)} · ${number(first)}–${number(last)} 秒</small></div><svg viewBox="0 0 670 220" role="img" aria-label="${escape(label(channel.name))}两次实测轨迹"><text x="2" y="22">${number(hi)}</text><text x="2" y="207">${number(lo)}</text><path d="M40 25V190H645" fill="none" stroke="#cbd5e1"/><path d="${path(1)}" fill="none" stroke="#2563eb" stroke-width="2"/><path d="${path(2)}" fill="none" stroke="#d97706" stroke-width="2"/></svg>`;
 }
 dispose(){this.alive=false;this.generation++;this.dialog?.close();}
}

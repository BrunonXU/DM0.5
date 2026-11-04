import {renderReplayReport} from './replay-report.js';
// Read-only preparation; motion is submitted only by an explicit start or align click.
const drafts=new Map();
const running=a=>['accepted','queued','running','stopping'].includes(a?.status);
const escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number=n=>Number.isFinite(n)?n.toFixed(3):'—';
const label=n=>n.startsWith('arm_')?`${n.startsWith('arm_l')?'左臂':'右臂'} J${n.split('_j')[1]}`:n.startsWith('left')?'左夹爪':'右夹爪';
const eventName=n=>({home_started:'Home 开始',home_stopped:'Home 停止',home_stage_completed_feedback:'Home 阶段到位',home_completed_feedback:'Home 到位',demonstration_stopped:'录制结束'})[n]||n;
export const motionReplayHTML=e=>e.source!=='unified_dataset'?'<section class="motion-replay" data-motion-replay hidden></section>':'';

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
  this.root.innerHTML=`<div class="card-header replay-compact-heading"><div><h3>实机复现</h3><span class="replay-mode">整条 · 轨迹复现 · 单次</span></div><button class="button small" data-r-close>收起</button></div>
   <div class="replay-control-line"><div class="replay-summary"><div data-r-review aria-live="polite"></div><p class="replay-mode replay-statistics"><strong data-r-duration>—</strong> 秒 <span>· <span data-r-count>—</span> 条命令</span></p></div>
   <div class="replay-buttons"><button class="button" data-r-recheck>检查起点</button><button class="button primary" data-r-start-task>开始回放</button><button class="button replay-force" data-r-force-task hidden disabled>强行 Replay</button><button class="button" data-r-align disabled>回到录制起点</button><button class="button danger" data-r-stop disabled>停止</button></div></div>
   <p class="replay-force-note" data-r-force-note hidden>跳过手臂起点偏差检查，从当前位置执行。</p>
   <div data-r-status aria-live="polite"></div><div data-r-comparison></div>
   <details data-r-more><summary>区间与诊断 · <span data-r-range-label>整条录制</span></summary><div class="replay-range"><label>开始（秒）<input data-r-start type="number" min="0" step="any" value="${escape(this.draft.start)}"></label><label>结束（留空至末尾）<input data-r-end type="number" min="0" step="any" value="${escape(this.draft.end)}"></label><button class="button" data-r-check>重新检查</button></div><div data-r-details></div><button class="button small" data-r-release hidden>释放控制权</button><p class="section-note">检查只读；回到起点与回放分别点击执行。软件停止不等于硬件急停，失联后不会自动续跑。</p></details>`;
  this.q('[data-r-close]').onclick=()=>{this.draft.open=false;this.root.hidden=true;this.hooks.expanded?.(false);};
  for(const name of ['start','end'])this.q('[data-r-'+name+']').oninput=e=>{this.draft[name]=e.target.value;this.review=null;this.generation++;this.q('[data-r-review]').textContent='区间已修改 · 开始前重新检查';this.q('[data-r-duration]').textContent='—';this.q('[data-r-count]').textContent='—';this.q('[data-r-range-label]').textContent='自定义区间';this.update();};
  this.q('[data-r-check]').onclick=()=>this.check();this.q('[data-r-recheck]').onclick=()=>this.check();
  this.q('[data-r-align]').onclick=()=>this.begin(true);
  this.q('[data-r-start-task]').onclick=()=>this.begin(false);
  this.q('[data-r-force-task]').onclick=()=>this.begin(false,true);
  this.q('[data-r-release]').onclick=()=>this.hooks.run(this.hooks.release);
  this.q('[data-r-stop]').onclick=()=>this.hooks.run(async()=>{const a=this.hooks.state()?.action;if(a?.replay?.episode_id!==this.episode.id)return;this.boundaryCancelled=this.boundary(a);await this.hooks.stop();this.update();});
  this.update();this.check();this.loadRecent();
 }
 boundary(action){return (action?.progress_detail?.phase||action?.phase||action?.result?.phase)==='head_boundary_observation';}
 invalidate(reason='状态已变化，请重新检查'){this.invalidReason=reason;this.review=null;this.generation++;this.busy=false;if(this.dialog){this.dialog.querySelector('[type=submit]').disabled=true;this.dialog.querySelector('[data-r-error]').textContent='状态或检查已失效，请关闭后重新检查';}}
 async check(){
  if(this.dialog||this.submitting||this.hooks.online?.()===false)return null;
  const generation=++this.generation;this.review=null;this.invalidReason=null;this.busy=true;this.update();
  const holder=this.q('[data-r-review]');holder.textContent='正在检查数据与起点…';
  try{
   const params=new URLSearchParams({episode_id:this.episode.id,start_s:this.draft.start||'0'});
   if(this.draft.end!=='')params.set('end_s',this.draft.end);
   const {review}=await this.hooks.api('/api/replay/review?'+params,undefined,{timeout:30000});
   if(!this.alive||generation!==this.generation)return null;
   this.q('.replay-mode').textContent=(this.draft.end!==''||Number(this.draft.start)>0?'所选区间':'整条')+' · 轨迹复现 · 单次';
   this.review=review;this.expires=Date.now()+(review.valid_for_s||0)*1000;
   holder.innerHTML=review.executable?'<span class="replay-state is-ready" data-r-ready>起点已就绪</span>':`<span class="replay-state is-blocked">${review.can_align||review.can_force_start?'需要对齐起点':'暂不可回放'}</span><p class="replay-blockers">${escape(review.issues?.[0]||'')}</p>`;
   this.q('[data-r-duration]').textContent=number(review.duration_s);
   this.q('[data-r-count]').textContent=Number.isFinite(review.command_count)?review.command_count.toLocaleString():'—';
   this.q('[data-r-range-label]').textContent=Number(this.draft.start||0)===0&&this.draft.end===''?'整条录制':`${number(review.start_s)}–${number(review.end_s)} 秒`;
   this.q('[data-r-details]').innerHTML=`${(review.issues||[]).map(issue=>`<p>${escape(issue)}</p>`).join('')}<p class="replay-detail-range">${number(review.start_s)}–${number(review.end_s)} 秒 · ${review.command_count} 条原始命令</p><p class="section-note">时间从首条命令算起；两次实测按各自反馈时间对齐，图像时间可能略有偏移。</p>${review.events?.length?`<details class="replay-events"><summary>历史事件 <span>${review.events.length} 条</span></summary><ul>${review.events.map(e=>`<li><time>${number(e.time_s)}s</time><span>${escape(eventName(e.event))}${e.reason?' · '+escape(e.reason):''}</span></li>`).join('')}</ul></details>`:''}`;
   return review;
  }catch(error){if(this.alive&&generation===this.generation)this.invalidReason=error.message;return null;}
  finally{if(this.alive&&generation===this.generation){this.busy=false;this.update();}}
 }
 update(){
  if(!this.alive||!this.draft.open)return;
  const state=this.hooks.state(),lease=this.hooks.lease(),action=state?.action,active=running(action),own=action?.replay?.episode_id===this.episode.id;
  const online=this.hooks.online?.()!==false,source=this.hooks.source?.()??this.episode.source_signature,context=source+'|'+(state?.control?.lease_id||'');
  const changed=this.context!==undefined&&this.context!==context;this.context=context;
  if(!online){if(this.online!==false)this.invalidate();this.online=false;}
  else {const reconnect=this.online===false;this.online=true;if(changed&&!this.intent&&!this.submitting)this.invalidate();if(reconnect&&!active&&!this.dialog&&!this.intent&&!this.submitting)queueMicrotask(()=>{if(this.alive&&!this.busy&&!this.dialog)this.check();});}
  if(source!==this.episode.source_signature&&this.review)this.invalidate();
  const expired=this.review?.review_id&&Date.now()>=this.expires;if(expired)this.invalidate('检查已过期，请重新检查');
  const busy=!!(this.busy||this.submitting||this.intent||this.dialog||active||this.hooks.executing());
  const check=this.q('[data-r-recheck]'),alignButton=this.q('[data-r-align]'),start=this.q('[data-r-start-task]'),force=this.q('[data-r-force-task]'),stop=this.q('[data-r-stop]');
  for(const selector of ['[data-r-start]','[data-r-end]','[data-r-check]'])this.q(selector).disabled=busy||!online;
  const ready=!!this.review?.executable,alignable=!!this.review?.can_align,forceable=!!this.review?.can_force_start;
  check.hidden=ready||alignable||!!(own&&active);check.disabled=busy||!online;check.textContent=this.busy?'检查中…':this.review?'重新检查':'检查起点';
  start.disabled=busy||!online||!ready;alignButton.disabled=busy||!online||!alignable;
  force.hidden=ready||!forceable;force.disabled=busy||!online||!forceable||ready;
  this.q('[data-r-force-note]').hidden=force.hidden;
  stop.disabled=!lease||!own||!active||action.status==='stopping';
  stop.textContent=action?.status==='stopping'?'正在停止':own&&this.boundary(action)?'取消后续动作':own&&action?.replay?.phase==='alignment'?'停止对齐':'停止回放';
  for(const button of [check,alignButton,start,stop])button.classList.remove('primary');
  (own&&active?stop:alignable?alignButton:ready?start:check).classList.add('primary');
  this.q('[data-r-release]').hidden=!lease;this.q('[data-r-release]').disabled=!!this.submitting||active;
  const readiness=this.q('[data-r-review]');readiness.hidden=!!(online&&own&&active);
  if(!online)readiness.textContent='状态失联';else if(!this.review&&!this.busy)readiness.textContent=this.invalidReason||'等待检查起点';
  const status=this.q('[data-r-status]');
  if(!online){status.textContent='';return;}
  if(own){
   const align=action.replay.phase==='alignment',p=action.progress_detail||{},names={accepted:'已受理',queued:'等待执行',running:align?'正在回到起点':'正在回放',stopping:'正在停止',cancelled:this.boundaryCancelled?'后续动作已取消':align?'对齐已中止':'回放已中止',rejected:'未执行',failed:'执行中断',succeeded:align?'已回到起点，请点击开始回放':'回放执行完成'};
   status.innerHTML=`<div class="replay-progress-heading"><strong>${escape(names[action.status]||action.status)}${action.replay.force_start?' · 强行 Replay':''}</strong>${active?`<span>${number(p.elapsed_s)}<small> / ${number(p.estimated_total_s??this.review?.duration_s)} 秒</small></span>`:''}</div>${active?`<progress aria-label="${align?'起点对齐':'回放'}进度" max="1" value="${Math.max(0,Math.min(1,typeof action.progress==='number'?action.progress:0))}"></progress>`:''}${action.error?`<p class="notice warn">${escape(action.error)}</p>`:''}`;
   if(align&&!active&&this.alignedAction!==action.id){this.alignedAction=action.id;queueMicrotask(()=>{if(this.alive&&!this.busy&&!this.dialog)this.check();});}
   if(!align&&!active&&action.replay.report_id&&this.reportId!==action.replay.report_id){this.reportId=action.replay.report_id;this.loadReport(this.reportId);}
  }else status.textContent=active?'其他动作正在执行，请等待结束。':'';
 }
 async begin(align,forceStart=false){
  if(this.intent||this.submitting||this.dialog||this.hooks.online?.()===false||running(this.hooks.state()?.action))return;
  const intentGeneration=this.hooks.intentGeneration?.();
  this.intent=true;this.update();
  try{
   if(!this.hooks.lease())await this.hooks.acquire();
   if(intentGeneration!==this.hooks.intentGeneration?.()){this.invalidate('操作已取消，请重新检查');return;}
   if(!this.alive||!this.hooks.lease())return;
   const review=await this.check();
   if(intentGeneration!==this.hooks.intentGeneration?.()){this.invalidate('操作已取消，请重新检查');return;}
   if(!review||!review[align?'can_align':forceStart?'can_force_start':'executable'])return;
   await this.submit(review,align,forceStart,intentGeneration);
  }catch(error){this.review=null;this.invalidReason=error.message;}finally{this.intent=false;this.update();}
 }
 async submit(review,align,forceStart=false,intentGeneration=this.hooks.intentGeneration?.()){
  if(this.submitting||!this.alive||this.hooks.online?.()===false||this.review!==review||Date.now()>=this.expires||intentGeneration!==this.hooks.intentGeneration?.()){this.invalidate();return;}
  this.hooks.pauseHistory?.();this.submitting=true;this.update();
  try{
   const {action}=await this.hooks.api('/api/replay/'+(align?'align':'start'),{review_id:review.review_id,lease_id:this.hooks.lease(),confirm:true,head_boundary_recovery_ack:!!(align&&review.alignment_plan?.head_boundary_recovery),...(!align&&forceStart?{force_start:true}:{})},{timeout:30000});
   this.boundaryCancelled=false;this.hooks.accept(action);
  }finally{this.submitting=false;this.review=null;if(this.alive)this.update();await this.hooks.refresh();}
 }
 async loadRecent(){
  try{const {reports}=await this.hooks.api('/api/replay/reports?'+new URLSearchParams({episode_id:this.episode.id,limit:'1'}));if(!this.alive||this.reportId||!reports?.length)return;this.reportId=reports[0].report_id;this.recentMeta=reports[0];await this.loadReport(this.reportId);}catch(error){if(this.alive&&!this.reportId){const holder=this.q('[data-r-comparison]');holder.innerHTML=`<p>${escape(error.message)} <button class="button small" data-r-recent-retry>重新读取上次结果</button></p>`;holder.querySelector('button').onclick=()=>this.loadRecent();}}
 }
 async loadReport(id){
  if(this.reportLoading===id)return;this.reportLoading=id;
  const holder=this.q('[data-r-comparison]');holder.textContent='正在读取实测对比…';
  try{
   const {report}=await this.hooks.api('/api/replay/reports/'+id);
   if(!this.alive||this.reportId!==id)return;this.loadedReportId=id;
   renderReplayReport(holder,report);
   // Preserve the separately developed report renderer; this workspace controls disclosure defaults.
   const table=holder.querySelector('.rr-details');if(table)table.open=true;
   const inspector=holder.querySelector('.rr-inspector');if(inspector){const details=document.createElement('details');details.className='replay-disclosure';details.innerHTML='<summary>轨迹与时间诊断</summary>';inspector.before(details);details.append(inspector);}
   holder.querySelectorAll('[data-rr-joint]').forEach(button=>button.addEventListener('click',()=>{const details=holder.querySelector('.replay-disclosure');if(details)details.open=true;}));
   const req=report.request||{},result=report.result||{};
   const sums=report.comparison?.arms_summary;const aggregate=sums?`<div class="table-scroll replay-aggregate"><table><thead><tr><th>手臂统计</th><th>关节数</th><th>MAE °</th><th>RMS °</th><th>最大偏差 °</th></tr></thead><tbody>${[['both','双臂'],['left','左臂'],['right','右臂']].map(([key,name])=>{const r=sums[key];return `<tr><th>${name}</th><td>${r?.joints_compared??'—'} / ${r?.joints_total??'—'}</td><td>${number(r?.metrics?.mean_abs)}</td><td>${number(r?.metrics?.rms)}</td><td>${number(r?.metrics?.max_abs)}</td></tr>`;}).join('')}</tbody></table></div>`:'';
   holder.insertAdjacentHTML('afterbegin',aggregate);
   holder.insertAdjacentHTML('afterbegin',`<p class="section-note" data-r-result-meta>${req.source_signature&&req.source_signature!==this.episode.source_signature?'历史来源 · ':''}最近结果${req.force_start?' · 强行 Replay':''} · ${escape(({succeeded:'执行完成',cancelled:'已中断',failed:'执行失败',rejected:'未执行'})[result.status]||'结果状态未记录')} · 任务用时 ${number(result.elapsed_s)} 秒 · 录制区间 ${number(req.end_s-req.start_s)} 秒</p>`);
   holder.querySelectorAll('th').forEach(th=>{if(/有效.*采样|比较覆盖/.test(th.textContent))th.textContent='反馈匹配 / 采样点';});
  }catch(error){if(this.alive&&this.reportId===id){holder.innerHTML=`<p>对比记录暂不可用：${escape(error.message)}</p><button class="button small" data-r-report-retry>重新读取</button>`;holder.querySelector('button').onclick=()=>this.loadReport(id);}}
  finally{if(this.reportLoading===id)this.reportLoading=null;}
 }
 dispose(){this.q('[data-r-comparison]')?._replayReportResize?.disconnect();this.alive=false;this.generation++;this.dialog?.close();}
}

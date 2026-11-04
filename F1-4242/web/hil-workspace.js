import {hilModeCards,hilOperationHTML,connectionRows} from './capture-ui.js';
/* Approved horizontal HIL workspace. Status/actions remain owned by HILPanel. */
import {cameraHTML} from './camera-preview.js';
import {teachingHTML} from './hil-teaching.js';
const tabs=[['live','运行现场','screen'],['connect','连接与启动','terminal'],['teaching','手柄教学','pad'],['diagnostic','诊断','pulse']];
const paths={zoom:'<path d="M9 3H3v6m12-6h6v6M3 15v6h6m6 0h6v-6"/>',screen:'<rect x="3" y="3" width="18" height="14" rx="2"/><path d="m8 9 3 3 5-6M12 17v4m-4 0h8"/>',terminal:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 8 3 4-3 4m6 0h4"/>',pad:'<path d="M7 7h10c3 0 5 12 3 13-2 1-4-4-5-4H9c-1 0-3 5-5 4C2 19 4 7 7 7Z"/><path d="M7 10v5m-2-2.5h4m6-1h.1m2 2h.1"/>',pulse:'<path d="M2 12h5l3-8 4 16 3-8h5"/>',copy:'<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M15 8V3H3v13h5"/>',stop:'<rect x="5" y="5" width="14" height="14" rx="2"/>',arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>'};
const icon=name=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">${paths[name]||paths.arrow}</svg>`;
const detail=(title,body)=>`<details class="hil-diagnostic-group"><summary>${title}<span aria-hidden="true">＋</span></summary>${body}</details>`;
export function workspaceHTML(panel){
 if(!panel.route)return `<div id="hil-root" class="hil-horizontal hil-home"><div id="hil-active-session"></div><h2>选择任务流程</h2><p class="section-note">选择只切换操作指引；开始与接管由现场手柄完成。</p><div id="hil-route-menu" class="hil-route-menu">${hilModeCards()}</div><div class="capture-help-links"><a href="http://192.0.2.1:4243/?doc=f1-startup" target="_blank" rel="noopener noreferrer">${icon('terminal')}<span><strong>启动与操作教学</strong><small>查看连接步骤、按键与保存流程</small></span>↗</a><a href="#hil?route=demonstration&amp;view=diagnostic">${icon('pulse')}<span><strong>连接诊断</strong><small>检查运行状态与最近消息</small></span>→</a></div></div>`;
 const title=panel.route==='play'?'自由接入':panel.route==='policy'?(panel.guideModel==='rlt400'?'局部 RLT':'DAgger 纠正'):'人工示教';
 let camera=cameraHTML('现场视野','');
 camera=camera.replace('<span class="live-label"> · 收起暂停</span>',`<span class="hil-camera-actions"><button class="button" data-hil-camera-info aria-expanded="false">画面信息</button><button class="button" data-hil-camera-toggle>收起画面</button></span>`);
 camera=camera.replaceAll('<span class="camera-overlay">',`<span class="hil-zoom-hint" aria-hidden="true">${icon('zoom')}</span><span class="camera-overlay">`);
 const teaching=teachingHTML();
 return `<div id="hil-root" class="hil-horizontal">
 <nav class="capture-routes" aria-label="HIL 采集流程">${hilModeCards([],panel.route==='policy'?panel.guideModel:panel.route,true,new URLSearchParams(location.hash.split('?')[1]||'').get('view')||'connect')}</nav><div class="hil-workspace-heading"><a class="button hil-back" href="#hil" aria-label="选择流程">←</a><h1 id="hil-route-name">${title}</h1><span id="hil-execution" class="hil-execution"></span><span id="hil-route-readiness" hidden></span><button class="button" data-hil-request-finish disabled>${icon('stop')}结束运行端</button></div>
 <div class="hil-featurebar"><div role="tablist" aria-label="HIL 功能">${tabs.map(([id,label,name])=>`<button class="button" id="hil-tab-${id}" data-hil-tab="${id}" role="tab" aria-controls="hil-pane-${id}" aria-selected="false">${icon(name)}${label}</button>`).join('')}</div></div>
 <div id="hil-persistent-status" role="status" hidden></div>
 <section id="hil-pane-live" data-hil-pane="live" role="tabpanel" aria-labelledby="hil-tab-live">
 <section class="card hil-overview tone-idle" id="hil-overview" aria-live="polite"><h2>状态读取中</h2></section>
 ${camera}<div id="hil-camera-info" hidden>预览目标 10 fps · 原始采集 30 Hz · 收起画面暂停预览</div><div id="hil-live-health"></div>
 <section class="card hil-session" id="hil-recording-session" hidden></section>
 </section>
 <section id="hil-pane-connect" data-hil-pane="connect" role="tabpanel" aria-labelledby="hil-tab-connect" hidden><div class="capture-connect-grid"><div class="capture-connect-main"><div class="hil-section-heading"><div><h2>${panel.singleWindow?'Windows 单窗口启动':'连接与启动'}</h2><p class="section-note">${panel.singleWindow?'在连接 F710 的电脑上运行这一条命令':'按顺序准备运行端、连接与手柄输入'}</p></div><button class="button" data-hil-all-commands aria-expanded="false">全部命令</button></div><div id="hil-command-tabs" class="hil-command-tabs" aria-label="启动步骤"></div><div id="hil-start"><div id="hil-commands"></div></div><div class="hil-command-navigation"><button class="button" data-hil-previous>上一步</button><button class="button primary" data-hil-next>下一步 ${icon('arrow')}</button></div></div><aside id="hil-connect-progress" class="capture-connection" aria-live="polite"></aside></div>${hilOperationHTML(panel)}</section>
 <section id="hil-pane-teaching" data-hil-pane="teaching" role="tabpanel" aria-labelledby="hil-tab-teaching" hidden><div class="hil-section-heading"><h2>手柄教学</h2><div class="hil-shortcuts"><button class="button" data-hil-lesson="start">START</button><button class="button" data-hil-lesson="rt">夹爪</button><button class="button" data-hil-lesson="y">复位</button></div></div>${panel.route==='policy'&&panel.guideModel==='rlt400'?'<div class="notice">局部 RLT：→进入局部，↑确认局部成功；Y 回位稳定后封存整条。↑不等于保存整条。</div>':''}${teaching}</section>
 <section id="hil-pane-diagnostic" data-hil-pane="diagnostic" role="tabpanel" aria-labelledby="hil-tab-diagnostic" hidden><div class="hil-section-heading"><h2>诊断与消息</h2><div><button class="button" data-hil-refresh>刷新</button> <button class="button primary" data-hil-copy="diagnostic">${icon('copy')}复制诊断摘要</button></div></div><div class="hil-diagnostic-columns"><section class="hil-diagnostic-card"><h3>连接与记录</h3><dl id="hil-diagnostic-health"></dl>${detail('会话与版本','<dl id="hil-diagnostic-session"></dl>')}${detail('采集与预览指标','<dl id="hil-diagnostic-metrics"></dl>')}</section><section class="hil-diagnostic-card"><h3>最近消息</h3><div id="hil-events"></div>${detail('原始原因','<pre id="hil-diagnostic-reason"></pre>')}${detail('完整诊断摘要','<pre id="hil-diagnostic"></pre>')}</section></div>${panel.route==='play'?'':'<details class="hil-diagnostic-card hil-recent"><summary>介入视频与审核 <span aria-hidden="true">＋</span></summary><div id="hil-clips">暂无介入记录</div><div id="hil-player"></div></details>'}</section>
 </div>`;
}
export function commandHTML(panel,r){
 const e=panel.esc;
 return (r.commands||[]).map((c,i)=>`<details open class="hil-step" data-hil-command="${e(c.id)}"><summary>${i+1} · ${e(c.where)}</summary><div class="hil-command-header"><strong>${e(c.where)}</strong><span>${panel.singleWindow?'PowerShell':i===0?'bash':'PowerShell'}</span><button class="button" data-hil-copy="${e(c.id)}" ${!c.command||(i===0&&!r.ready)?'disabled':''}>${icon('copy')}复制命令</button></div><div class="hil-step-content"><pre>${e(c.command||'等待后端安装')}</pre><p>${e(c.hint||'')}</p></div></details>`).join('');
}
export class HILWorkspace{
 constructor(panel){
  this.panel=panel;this.root=document.querySelector('#hil-root');this.step=0;this.all=false;
  this.page=new URLSearchParams(location.hash.split('?')[1]||'').get('view');this.explicit=tabs.some(t=>t[0]===this.page);if(!this.explicit)this.page='live';
  this.handler=event=>this.click(event);this.root.addEventListener('click',this.handler);
  this.keyHandler=event=>{const button=event.target.closest('[data-hil-tab]');if(!button||!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const i=tabs.findIndex(t=>t[0]===button.dataset.hilTab),n=event.key==='Home'?0:event.key==='End'?tabs.length-1:(i+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;this.select(tabs[n][0]);this.root.querySelector(`[data-hil-tab="${tabs[n][0]}"]`).focus();};this.root.addEventListener('keydown',this.keyHandler);
  this.camera=this.root.querySelector('[data-camera-preview]');this.cameraToggle=()=>{this.root.querySelector('[data-hil-camera-toggle]').textContent=this.camera.open?'收起画面':'展开画面';};this.camera?.addEventListener('toggle',this.cameraToggle);
  if(panel.route)this.select(this.page,false);
 }
 select(page,explicit=true){
  for(const a of this.root.querySelectorAll('.capture-routes a')){const q=new URL(a.href).hash;const params=new URLSearchParams(q.split('?')[1]);a.setAttribute('aria-current',params.get('route')===this.panel.route&&(this.panel.route!=='policy'||params.get('model')===this.panel.guideModel)?'page':'false');}
  this.page=page;if(explicit)this.explicit=true;
  for(const el of this.root.querySelectorAll('[data-hil-pane]'))el.hidden=el.dataset.hilPane!==page;
  for(const el of this.root.querySelectorAll('[data-hil-tab]')){const on=el.dataset.hilTab===page;el.setAttribute('aria-selected',String(on));el.tabIndex=on?0:-1;}
  for(const b of document.querySelectorAll('[data-context-hil]'))b.setAttribute('aria-current',b.dataset.contextHil===page?'page':'false');
  const params=new URLSearchParams(location.hash.split('?')[1]||'');params.set('view',page);if(this.explicit)history.replaceState(null,'','#hil?'+params);
  if(page!=='teaching')this.panel.teaching?.stop();
  else if(!this.panel.teaching?.key)requestAnimationFrame(()=>{if(this.page==='teaching'&&!this.panel.teaching?.key)this.panel.teaching?.select('rb');});
  this.update();
 }
 click(event){
  const button=event.target.closest('button');if(!button)return;
  if(button.dataset.hilTab)this.select(button.dataset.hilTab);
  if(button.hasAttribute('data-hil-step-select')){this.step=Number(button.dataset.hilStepSelect);this.all=false;this.updateCommands();}
  if(button.hasAttribute('data-hil-all-commands')){this.all=!this.all;this.updateCommands();}
  if(button.hasAttribute('data-hil-next')){this.step++;this.updateCommands();}
  if(button.hasAttribute('data-hil-previous')){this.step--;this.updateCommands();}
  if(button.dataset.hilLesson)this.panel.teaching?.select(button.dataset.hilLesson);
  if(button.hasAttribute('data-hil-refresh'))this.panel.refresh();
  if(button.hasAttribute('data-hil-camera-info')){event.preventDefault();const info=this.root.querySelector('#hil-camera-info');info.hidden=!info.hidden;this.camera.classList.toggle('hil-show-camera-info',!info.hidden);button.setAttribute('aria-expanded',String(!info.hidden));}
  if(button.hasAttribute('data-hil-camera-toggle')){event.preventDefault();this.camera.open=!this.camera.open;}
  if(button.hasAttribute('data-hil-request-finish'))this.confirmFinish(button);
  const saved=event.target.closest('[data-hil-saved]');if(saved)this.showSaved(saved);
 }
 updateCommands(){
  const commands=this.panel.currentCommands||[],holder=this.root.querySelector('#hil-command-tabs');if(!holder)return;
  if(this.panel.singleWindow){holder.hidden=true;this.root.querySelector('[data-hil-all-commands]').hidden=true;this.root.querySelector('.hil-command-navigation').hidden=true;return;}
  holder.hidden=false;
  this.root.querySelector('[data-hil-all-commands]').hidden=false;this.root.querySelector('.hil-command-navigation').hidden=false;
  this.step=Math.max(0,Math.min(this.step,commands.length-1));
  const key=JSON.stringify(commands.map(c=>[c.id,c.where]));if(holder.dataset.key!==key){holder.dataset.key=key;holder.innerHTML=commands.map((c,i)=>`<button class="button" data-hil-step-select="${i}" aria-controls="hil-commands"><span class="hil-step-number">${i+1}</span><span><strong>${['F1 运行端','Windows 隧道','F710 手柄'][i]||this.panel.esc(c.where)}</strong><small data-hil-step="${i}">待启动</small></span></button>`).join('');}
  const oldLabels=this.panel.stepLabels||[];
  holder.querySelectorAll('[data-hil-step]').forEach((el,i)=>{el.textContent=oldLabels[i]||'待启动';el.classList.toggle('complete',!!this.panel.rootSteps?.[i]);});
  holder.querySelectorAll('button').forEach((el,i)=>el.setAttribute('aria-pressed',String(this.step===i&&!this.all)));
  this.root.querySelectorAll('#hil-commands > [data-hil-command]').forEach((el,i)=>{el.open=true;el.hidden=!this.all&&i!==this.step;});
  const all=this.root.querySelector('[data-hil-all-commands]');all.textContent=this.all?'逐步查看':'全部命令';all.setAttribute('aria-expanded',String(this.all));
  this.root.querySelector('[data-hil-previous]').hidden=this.all||this.step===0;
  const next=this.root.querySelector('[data-hil-next]');next.hidden=this.all||this.step>=commands.length-1;next.innerHTML=`下一步 · ${['F1 运行端','Windows 隧道','F710 手柄'][this.step+1]||''} ${icon('arrow')}`;
 }
 setText(selector,value){const el=this.root.querySelector(selector);if(el&&el.textContent!==value)el.textContent=value;}
 setRows(selector,rows){const el=this.root.querySelector(selector);if(!el)return;const html=rows.map(([k,v])=>`<div><dt>${this.panel.esc(k)}</dt><dd>${this.panel.esc(v===undefined||v===null?'未确认':String(v))}</dd></div>`).join('');if(el.innerHTML!==html)el.innerHTML=html;}
 update(){
  const p=this.panel;if(!p.route)return;
  const d=p.data||{},s=d.status||{},record=s.recording||{},live=p.live(),same=p.actualRoute()===p.route;
  if(!this.explicit&&p.online&&p.data){this.explicit=true;this.select(d.active?'live':'connect',false);}
  this.updateCommands();
  const progress=this.root.querySelector('#hil-connect-progress'),connection=p.getConnection?.()||{},matching=same&&!p.guideMismatch();
  if(progress){const rows=connectionRows([['机器人连接',connection.online?(connection.connected?'已连接':'等待反馈'):'状态未确认',connection.online&&connection.connected?'good':'','arm'],['运行端',!p.online?'状态失联':!matching&&d.active?'其他流程运行中':live?'运行端就绪':d.active?'准备中':'未启动',live&&matching?'good':'','screen'],['手柄输入',live&&matching?(s.input_fresh?'已收到':'输入中断'):'未确认',live&&matching&&s.input_fresh?'good':'','pad']]);const title=!p.online?'连接中断':!matching&&d.active?'其他流程运行中':live?(s.input_fresh?'连接准备完成':'等待手柄输入'):d.active?'正在准备':'等待启动';const html=`<h2>连接进度</h2><h3>${p.esc(title)}</h3>${rows}<p>${live&&matching?'按下方实体按键说明操作；运行状态以现场反馈为准。':'下一步：运行左侧启动命令，查看连接进度。'}</p><span class="capture-physical-key">实体手柄 <kbd>START</kbd></span>`;if(progress.innerHTML!==html)progress.innerHTML=html;}
  const overview=this.root.querySelector('#hil-overview');
  // Keep the existing status, Home-stop, input-pause and error rendering intact.
  if(live&&same&&p.route==='demonstration'&&['hil_latched','manual_hold','demonstration_ready','episode_ready'].includes(d.phase)&&!s.home_stage&&!s.home_stop_reason){const title=overview.querySelector('h2'),label={idle:'等待开始录制',recording:'正在录制',starting:'准备录制',saving:'正在保存',failed:'保存异常'}[record.phase];if(title&&label)title.textContent=label;}
  const health=overview.querySelector('.hil-health');if(health)this.root.querySelector('#hil-live-health').replaceChildren(health);else if(!p.online)this.root.querySelector('#hil-live-health').replaceChildren();
  // Additional elements are rebuilt with the status card, never from mock values.
  overview.querySelector('.hil-record-count')?.remove();overview.querySelector('.hil-live-instruction')?.remove();
  if(live&&same&&p.route==='demonstration'&&Number.isFinite(record.completed)){const count=document.createElement('div');count.className='hil-record-count';count.innerHTML=`<strong>${record.completed.toLocaleString('zh-CN')}</strong> 帧<small>已写盘</small>`;overview.append(count);}
  if(live&&same&&!overview.querySelector('.hil-next')&&!s.home_stage&&!s.home_stop_reason){const hint=document.createElement('p');hint.className='hil-live-instruction';hint.innerHTML=p.route==='policy'?(p.actualRLT()?'START 开条 / 接管 / 交回 VLA · →进入局部<br>↑确认局部成功 · Y 回位稳定后保存整条':'START 开条 / 接管 · RB 控臂<br>释放控制后 ↑ 保存本条'):p.route==='play'?'按住 <kbd>RB</kbd> 控臂，松开保持。<br>自由接入不采集训练图像。':'收尾由您完成；松开 <kbd>RB</kbd>，等待夹爪操作完成后<br>按 <kbd>↑</kbd> 保存，无需回位或固定计时。';overview.append(hint);}
  this.setText('#hil-execution',live?({POWERED:'实机',SHADOW:'演练'})[s.execution]||'未确认执行权限':'未确认执行权限');
  const finish=this.root.querySelector('[data-hil-request-finish]');const pending=p.finishPending||(Boolean(p.finishing)&&p.finishing===d.run_id)||d.completion?.phase==='finalizing';finish.innerHTML=`${icon('stop')}${pending?'正在结束…':'结束运行端'}`;if(pending&&overview.querySelector('h2'))overview.querySelector('h2').textContent='正在结束';finish.disabled=!live||!same||!d.can_finish||p.finishPending||(Boolean(p.finishing)&&p.finishing===d.run_id)||d.completion?.phase==='finalizing';
  const status=this.root.querySelector('#hil-persistent-status');status.hidden=this.page==='live';
  const alerts=[...overview.querySelectorAll('.hil-session-mismatch, .notice, .hil-writer-error')];const alert=alerts.length>0;
  const label=overview.querySelector('h2')?.textContent||'状态读取中';
  const warning=[...alerts.map(el=>el.textContent),overview.querySelector('.hil-next')?.textContent||'',live&&s.limit_reason?'方向受限：'+s.limit_reason:''].filter(Boolean).join(' · ');
  const summary=[label,live&&same&&Number.isFinite(record.completed)?`${record.completed.toLocaleString('zh-CN')} 帧已写盘`:'',warning].filter(Boolean).join(' · ');
  if(status.textContent!==summary)status.textContent=summary;status.className=alert||(live&&s.limit_reason)||/fault|waiting/.test(overview.className)?'hil-status-warning':'';
  const session=this.root.querySelector('#hil-recording-session');session.dataset.active=String(!!d.active);const help=session.querySelector('.hil-session-help summary');if(help)help.textContent='录制操作说明';
  // Preserve the original complete recording instructions as an explicit disclosure.
  for(const row of session.querySelectorAll('.hil-last-saved'))for(const node of [...row.childNodes])if(node.nodeType===Node.TEXT_NODE)node.textContent='上一条已保存';
  for(const link of session.querySelectorAll('a[href^="#data?episode="]')){const button=document.createElement('button');button.className='button';button.dataset.hilSaved=link.getAttribute('href');button.textContent='查看这条 →';link.replaceWith(button);}
  const events=this.root.querySelector('#hil-events');if(!p.online&&events)events.textContent='连接中断，等待重新读取消息。';
  this.setRows('#hil-diagnostic-health',[['运行状态',p.online?(live?'运行中':d.active?'状态未知':'未运行'):'连接中断'],['输入链路',live?(s.input_fresh?'新鲜':'中断'):'未确认'],['三路观测',live?(Object.values(s.camera_age_ms||{}).length===3&&Object.values(s.camera_age_ms).every(v=>Number.isFinite(v)&&v<200)?'新鲜':'检查观测'):'未确认'],['记录',live?(p.actualRoute()==='play'?'不采集训练图像':record.writer_error?'写盘异常':({idle:'未录制',starting:'准备中',recording:'正在录制',saving:'保存中',failed:'保存异常'})[record.phase]||'未确认'):'未确认']]);
  this.setRows('#hil-diagnostic-session',[['运行 ID',d.run_id],['实际流程',p.actualRoute()],['版本',d.release],['状态来源',d.source],['执行模式',live?s.execution:null]]);
  this.setRows('#hil-diagnostic-metrics',[['摘要年龄',Number.isFinite(d.summary_age_s)?`${d.summary_age_s.toFixed(1)} 秒`:'未确认'],['已写盘',live?record.completed:null],['预览目标','10 fps'],['采集目标',p.actualRoute()==='play'?'不采集训练图像':'30 Hz'],...['left','head','right'].map(k=>[`${{left:'左腕',head:'头部',right:'右腕'}[k]}观测年龄`,live&&Number.isFinite(s.camera_age_ms?.[k])?`${s.camera_age_ms[k]} ms`:null])]);
  this.setText('#hil-diagnostic-reason',[record.writer_error,record.last_rejection_reason,s.recovery_reason,s.recovery_error,s.hold_reason,s.warning,s.home_stop_reason,s.limit_reason,d.last_failure,...(d.completion?.errors||[])].filter(Boolean).join('\n')||'暂无异常原因');
  this.setText('#hil-diagnostic',p.data?JSON.stringify({panel_online:p.online,...p.data},null,2):'等待状态');
 }
 dialog(title,body,actions){
  this.modal?.remove();const dialog=document.createElement('dialog');dialog.className='hil-workspace-dialog';dialog.innerHTML=`<h2>${title}</h2><div class="hil-dialog-body">${body}</div><div class="hil-dialog-actions"><button class="button" data-cancel>取消</button>${actions}</div>`;document.body.append(dialog);dialog.querySelector('[data-cancel]').onclick=()=>dialog.close();dialog.addEventListener('close',()=>{this.origin?.focus({preventScroll:true});dialog.remove();});this.modal=dialog;dialog.showModal();return dialog;
 }
 confirmFinish(origin){
  if(origin.disabled)return;const p=this.panel,key=structuredClone(p.data?.session_key);this.origin=origin;
  p.finish(key);
 }
 showSaved(origin){
  this.origin=origin;const p=this.panel,saved=p.data?.status?.recording?.last_saved||p.data?.completion;
  const dialog=this.dialog('已保存记录',`<p>${p.esc(saved?.run_id||saved?.episode_id||'')}</p><p>在数据页查看三路原图、审核和 Replay。</p>`,`<a class="button primary" href="${p.esc(origin.dataset.hilSaved)}">在数据页查看 ${icon('arrow')}</a>`);
  dialog.querySelector('[data-cancel]').textContent='返回现场';dialog.querySelector('a').onclick=()=>dialog.close();
 }
 dispose(){this.root.removeEventListener('click',this.handler);this.root.removeEventListener('keydown',this.keyHandler);this.camera?.removeEventListener('toggle',this.cameraToggle);this.modal?.close();this.modal?.remove();}
}

import {captureIcon} from './capture-ui.js';
// Shared desktop layout. All operations remain in the existing app handlers.
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const button = (text, action, cls='') => `<button type="button" class="button ${cls}" data-click="${action}">${text}</button>`;
const row = (name, value) => `<div class="line-item"><span class="label">${esc(name)}</span><span class="value">${esc(value ?? '—')}</span></div>`;

export function overviewHTML(heading, cameraPanel, icon) {
 return `${heading('','总览','现场状态，一眼掌握',button(icon('system')+' 系统详情','system-details'))}
 <section class="overview-summary" aria-label="当前状态"><div><span>当前任务</span><strong id="overview-current-task">正在读取</strong></div><div><span>控制来源</span><strong id="overview-control-source">正在读取</strong></div><div><span>机器人反馈</span><strong id="overview-feedback">正在读取</strong></div></section>
 <div class="overview-layout"><div class="overview-scene">${cameraPanel('现场视野','')}</div>
 <aside class="stack overview-sidebar"><section class="card workbench-startup" aria-label="基础启动"><div><h2 id="startup-title">正在读取基础服务</h2><p id="startup-summary">等待本机状态</p></div><div class="workbench-actions"><button class="button primary" id="base-start-button" data-click="startup" data-start-base disabled>一键启动 <span class="start-all-label">Start All</span></button>${button('启动详情','startup-details','small')}</div></section>
 <section class="card workbench-devices"><div class="card-header"><h2>设备状态</h2><span id="health-summary"></span></div><div id="health-grid" class="health-grid"></div><details class="workbench-feedback"><summary>双臂角度 · 实测</summary><div id="joint-overview" class="feedback-columns mt"></div></details></section>
 <section class="card workbench-events"><div class="card-header"><h2>最近操作</h2><div id="overview-task"></div></div><div id="event-list" class="event-list"></div><details class="workbench-feedback"><summary>当前工作状态</summary><div id="work-status" class="line-list mt"></div><div id="overview-alerts"></div></details></section></aside></div>`;
}

export function startupPresentation(app, groups) {
  const s=app.startup, state=app.state||{};
  const fresh=e=>typeof e?.age_s==='number'&&Number.isFinite(e.age_s)&&e.age_s<=2;
  const names=Object.values(groups).flat();
  const joints=names.length>0&&names.every(n=>fresh(state.joints?.[n])&&Number.isFinite(state.joints[n].position));
  const cameras=['head','left','right'].every(n=>fresh(state.cameras?.[n]));
  const ready=app.apiOnline&&state.connected&&joints&&cameras;
  if(!app.startupOnline||!s)return {title:'基础服务状态待确认',summary:'暂未取得启动状态，请刷新后检查。',ready:false,canStart:false};
  const canStart=app.apiOnline&&s.can_start===true&&!app.startupBusy&&!app.startupUncertain;
  if(app.startupUncertain)return {title:'启动受理结果待核对',summary:'请查看启动详情；确认结果前不会重复发送。',ready:false,canStart:false};
  if(s.stage==='failed')return {title:'基础启动异常',summary:s.reason||s.message||'请查看启动详情。',ready:false,canStart};
  if(app.startupBusy||s.stage==='starting')return {title:'基础服务启动中',summary:'正在等待启动结果与设备反馈。',ready:false,canStart:false};
  if(ready)return {title:'基础服务已就绪',summary:'关节与三路相机反馈正常，可以开始工作。',ready:true,canStart:false};
  if(['running','completed'].includes(s.stage)||s.existing_pids?.length)return {title:'基础服务待检查',summary:s.stage==='completed'?'脚本已完成，设备反馈尚未全部就绪。':'已检测到基础节点，请查看下方设备状态。',ready:false,canStart:false};
  return {title:s.stage==='offline'?'离线开发环境':'启动基础服务',summary:s.can_start?'一键启动驱动与三路相机。':s.reason||s.message||'等待本机启动条件。',ready:false,canStart};
}

export function drawerHTML(kind, tab, app) {
  const title={system:'系统详情',startup:'启动详情',session:'操作会话'}[kind]||'系统详情';
  let body='';
  if(kind==='system')body=`<nav class="workbench-tabs" role="tablist" aria-label="系统详情分类">${[['identity','本机信息'],['connection','版本与连接']].map(([id,label])=>`<button type="button" role="tab" aria-selected="${tab===id}" tabindex="${tab===id?0:-1}" data-system-tab="${id}">${label}</button>`).join('')}</nav><section ${tab==='identity'?'':'hidden'}><h3>本机身份</h3><dl class="system-definition" id="system-identity"></dl><a class="button" id="vendor-link" href="#" target="_blank" rel="noopener noreferrer">打开原厂运维 ↗</a></section><section ${tab==='connection'?'':'hidden'}><h3>版本与连接</h3><div class="line-list">${row('浏览器入口',location.origin)}${row('后端版本',app.system?.version||app.robot?.version||'未读取')}<div class="line-item"><span class="label">资源版本</span><span class="value mono" id="revision-label">等待检测</span></div></div><h3 class="mt">服务状态</h3><div id="system-services" class="line-list"></div></section><div class="workbench-actions mt">${button('刷新状态','refresh')}${button('复制状态摘要','copy-workbench')}</div><div class="workbench-related"><h3>相关操作</h3><div class="workbench-actions mt">${button('启动详情','startup-details')}${button('操作会话','session-details')}</div></div>`;
  if(kind==='startup'){
    const s=app.startup||{};
    body=`<div class="workbench-state"><h3 id="startup-detail-title">正在读取</h3><p id="base-start-status">等待状态</p></div><div id="startup-result" class="line-list"></div><h3 class="mt">启动范围</h3><p class="section-note">驱动、运动学、三路相机，以及原升降、全身与手柄节点。EC 主站在现场独立启动；Pico、HIL 与录制分别启动。</p><details class="workbench-disclosure mt"><summary>现场终端命令</summary><div class="workbench-code"><div>bash ${button('复制命令','copy-startup','small')}</div><pre id="startup-command">${esc(startupCommand(s))}</pre></div></details><div class="workbench-actions mt"><button class="button primary" data-click="startup" data-start-base disabled>一键启动 Start All</button>${button('刷新状态','refresh')}${button('复制状态摘要','copy-workbench')}</div>`;
  }
  if(kind==='session')body=`<div class="workbench-state"><h3 id="session-heading">操作会话</h3><p class="section-note">操作无需密码。点击执行即提交，控制权由当前浏览器独立持有。</p></div><div id="session-status" class="line-list"></div><div class="workbench-actions mt">${button('释放控制权','release')}</div>`;
  return `<div class="workbench-drawer-heading"><h2 id="workbench-drawer-title">${title}</h2>${button('关闭','close-workbench','small')}</div>${body}`;
}

export function startupCommand(s) {
  // Quote configured paths; copying never executes these commands.
  const quote=p=>"'"+String(p).replace(/'/g,"'\\''")+"'";
  return s.script&&s.ec_script?`# EC 主站未运行时，在现场终端启动\nbash ${quote(s.ec_script)}\n# 另开终端；已有基础节点时不要重复运行\nbash ${quote(s.script)}`:'等待后端提供本机启动脚本路径';
}

export function startupResultHTML(app) {
  const s=app.startup||{},view=startupPresentation(app,app.robot?.groups||{});
  const labels={idle:'未运行',running:'检测到基础节点',completed:'脚本已完成',starting:'运行中',failed:'失败',offline:'离线'};
  return row('启动请求',app.startupUncertain?'受理结果待核对':app.startupAccepted?'已受理':'未在本浏览器发起')+row('启动脚本',app.startupOnline?labels[s.stage]||'待核对':'状态失联')+row('设备反馈',view.ready?'关节与三路图像正常':'尚未全部就绪')+row('脚本核验',app.startupOnline?(s.script_verified?'通过':'未通过'):'待核对')+row('脚本结果',s.result||'未报告')+row('退出码',s.exit_status)+row('已有基础进程',Array.isArray(s.existing_pids)?s.existing_pids.length:'未报告');
}

export const picoViews=[['live','运行现场'],['connect','连接与启动'],['record','独立录制'],['help','操作对照']];
export function mountPicoWorkspace() {
  const main=document.querySelector('#main'),grid=main.querySelector(':scope > .grid.two.mt');if(!grid)return;
  const [upper,lxl,recorder,compare]=[...grid.children],camera=main.querySelector('[data-camera-preview]'),session=main.querySelector('#teleop-session');
  const host=document.createElement('div');host.className='pico-workspace';
  host.innerHTML=`<div class="pico-workspace-bar"><nav class="workbench-tabs" role="tablist" aria-label="Pico 功能">${picoViews.map(([id,label])=>`<button type="button" id="pico-tab-${id}" role="tab" aria-controls="pico-view-${id}" data-pico-view="${id}">${label}</button>`).join('')}</nav><button type="button" class="button stop-button" data-teleop="stop" id="pico-persistent-stop" hidden>结束遥操</button></div><div class="pico-persistent-state" id="pico-persistent-state" hidden></div>
    <section id="pico-view-live" role="tabpanel" aria-labelledby="pico-tab-live" class="pico-live-grid"><div data-pico-camera-slot="live"></div><aside class="stack"><div data-pico-session-slot="live"></div><div data-pico-recorder-slot="live"></div><section class="card pico-record-link"><div class="card-header"><h2>独立录制</h2><span id="pico-record-summary" class="pill neutral">等待状态</span></div><p class="section-note">双臂与夹爪 · 三路相机</p><div class="workbench-actions mt"><button type="button" class="button primary" data-pico-view="record">打开录制</button><a class="button" href="#data">查看数据</a></div></section></aside></section>
    <section id="pico-view-connect" role="tabpanel" aria-labelledby="pico-tab-connect" hidden><h2 class="capture-flow-label">遥操方式</h2><nav class="pico-variant-options" aria-label="Pico 遥操方式">${[['upper','上肢版','双臂与夹爪','arm'],['lxl','LXL 原流程','沿用原有操作','book']].map(([id,title,note,glyph])=>`<button class="capture-mode" data-pico-variant="${id}" aria-pressed="${id==='upper'}">${captureIcon(glyph)}<span class="capture-mode-copy"><strong>${title}</strong><small>${note}</small></span></button>`).join('')}</nav><div class="capture-connect-grid"><div><h2>连接 Pico，准备遥操</h2><ol class="pico-onboarding"><li><span>1</span><div><strong>连接同一局域网</strong><p>Pico 与 F1 连入同一网络，在 Xrobotokit 选择 F1。</p></div></li><li><span>2</span><div><strong>开启数据发送</strong><p>启用 Head、Controller、Hand，然后点击 Send。</p></div></li><li><span>3</span><div><strong>释放控制，启动遥操</strong><p>松开 Grip 和扳机，摇杆回中；点击下方启动按钮。</p></div></li></ol><div id="pico-versions"></div></div><aside class="stack"><section id="pico-connect-progress" class="capture-connection" aria-live="polite"></section><div data-pico-session-slot="connect"></div></aside></div><section class="capture-operation" data-pico-upper-keys><div class="capture-operation-title"><strong>实体手柄操作</strong></div><div class="capture-operation-steps"><span><kbd>左／右 Grip</kbd>接管对应手臂</span><span><kbd>Trigger</kbd>控制夹爪</span><span><kbd>松 Grip</kbd>保持</span></div><p>左菜单键：重建局部参考。仅对应 Grip 接管时，Trigger 控制夹爪。</p><button class="button" data-pico-view="help">查看操作教学 ↗</button></section><div data-pico-recorder-slot="connect"></div></section>
    <section id="pico-view-record" role="tabpanel" aria-labelledby="pico-tab-record" class="pico-record-grid" hidden><div id="pico-recorder-slot" data-pico-recorder-slot="record"></div><div data-pico-camera-slot="record"></div></section>
    <section id="pico-view-help" role="tabpanel" aria-labelledby="pico-tab-help" hidden></section>`;
  // Keep the primary launch action ahead of optional implementation details.
  for(const card of [upper,lxl]){const actions=card.querySelector('.quick-links'),detail=document.createElement('details');detail.className='pico-setup-details';detail.innerHTML='<summary>版本说明与现场脚本</summary>';for(const child of [...card.children])if(child!==actions)detail.append(child);card.append(actions,detail);}
  upper.dataset.picoVariantPanel='upper';lxl.dataset.picoVariantPanel='lxl';host.querySelector('#pico-versions').append(upper,lxl);
  host.querySelector('[data-pico-recorder-slot=live]').append(recorder);
  host.querySelector('#pico-view-help').append(compare);
  host.querySelector('[data-pico-session-slot=live]').append(session);
  host.querySelector('[data-pico-camera-slot=live]').append(camera);
  grid.replaceWith(host);
  selectPicoVariant(new URLSearchParams(location.hash.split('?')[1]||'').get('variant')||'upper',false);
  return host;
}

export function selectPicoView(view, remember=true) {
  if(!picoViews.some(([id])=>id===view))view='live';
  const host=document.querySelector('.pico-workspace');if(!host)return;
  host.dataset.view=view;
  for(const [id] of picoViews){const active=id===view;document.querySelector('#pico-view-'+id).hidden=!active;const tab=document.querySelector('#pico-tab-'+id);tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;}
  const camera=host.querySelector('[data-camera-preview]'),session=host.querySelector('#teleop-session'),recorder=host.querySelector('#recorder-card');
  if(recorder)host.querySelector(`[data-pico-recorder-slot="${view==='record'?'record':view==='connect'?'connect':'live'}"]`).append(recorder);
  for(const b of document.querySelectorAll('#sidebar-context [data-pico-view]'))b.setAttribute('aria-current',b.dataset.picoView===view?'page':'false');
  if(camera){host.querySelector(`[data-pico-camera-slot="${view==='record'?'record':'live'}"]`).append(camera);if(view==='live'||view==='record')camera.open=true;}
  if(session)host.querySelector(`[data-pico-session-slot="${view==='connect'?'connect':'live'}"]`).append(session);
  const error=document.querySelector('#pico-request-error');if(error&&session)session.after(error);
  if(remember){const q=new URLSearchParams(location.hash.split('?')[1]||'');q.set('view',view);history.replaceState(null,'','#pico?'+q);}
}

export function selectPicoVariant(variant,remember=true){
 const host=document.querySelector('.pico-workspace');if(!host)return;
 if(!['upper','lxl'].includes(variant))variant='upper';host.dataset.variant=variant;
 for(const button of host.querySelectorAll('[data-pico-variant]'))button.setAttribute('aria-pressed',String(button.dataset.picoVariant===variant));
 for(const panel of host.querySelectorAll('[data-pico-variant-panel]'))panel.hidden=panel.dataset.picoVariantPanel!==variant;
 host.querySelector('[data-pico-upper-keys]').hidden=variant!=='upper';
 if(remember){const q=new URLSearchParams(location.hash.split('?')[1]||'');q.set('variant',variant);history.replaceState(null,'','#pico?'+q);}
}

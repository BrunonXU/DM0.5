import {MotionReplay,motionReplayHTML} from './motion-replay.js';
import {libraryView,libraryPage} from './data-library.js';
import {storageHTML} from './data-storage.js';
import {replayHTML,HILReplay} from './hil-replay.js';
import {CameraPreview,cameraHTML} from './camera-preview.js';
import {HILPanel} from './hil.js';
import {liftNames,liftRange,liftTarget,projectLift,presetPercent} from './lift-range.js';
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const finite = value => typeof value === 'number' && Number.isFinite(value);
const fmt = (value, precision = 1) => finite(value) ? value.toFixed(precision) : '—';
const iconPaths = {
  overview:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  control:'<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3" fill="currentColor"/><circle cx="16" cy="17" r="3" fill="currentColor"/>',
  pico:'<path d="M5 7h14l2 10-3 2-4-4h-4l-4 4-3-2z"/><path d="M7 10v4m-2-2h4m7-1h.01m2 2h.01"/>',
  data:'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v7c0 4 16 4 16 0V5M4 12v7c0 4 16 4 16 0v-7"/>',
  system:'<path d="m9 3-1 3-3 1-2 3 2 3v4l3 2 3-1 3 3 3-2v-4l3-2-1-4-4-1-1-3z"/><circle cx="11.5" cy="12" r="3"/>',
  camera:'<rect x="3" y="7" width="18" height="14" rx="3"/><path d="m7 7 2-4h6l2 4"/><circle cx="12" cy="14" r="4"/>',
  signal:'<path d="M3 10a14 14 0 0 1 18 0M6 14a9 9 0 0 1 12 0M9 18a4 4 0 0 1 6 0"/><path d="M12 21h.01"/>',
  arm:'<circle cx="6" cy="18" r="3"/><circle cx="13" cy="7" r="3"/><path d="m7 15 4-6m5-2h4v5m-2 0h4M3 21h8"/>',
  check:'<path d="m5 12 4 4L19 6"/>',
  arrow:'<path d="M5 12h14m-5-5 5 5-5 5"/>',
  download:'<path d="M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4"/>',
  refresh:'<path d="M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 13 3M18 18a8 8 0 0 1-13-3"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  eye:'<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
};
const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${iconPaths[name] || iconPaths.system}</svg>`;
const tabs = [ ['overview','总览','overview'], ['control','调姿','control'], ['pico','Pico 遥操','pico'], ['hil','HIL','signal'], ['data','数据','data'], ['system','系统配置','system'] ];
const fallbackGroups = {left_arm:Array.from({length:7},(_,i)=>`arm_l_j${i+1}`),right_arm:Array.from({length:7},(_,i)=>`arm_r_j${i+1}`),head:Array.from({length:3},(_,i)=>`head_j${i+1}`),body:Array.from({length:5},(_,i)=>`body_j${i+1}`)};
let stored = {};
try { stored = JSON.parse(sessionStorage.getItem('f1-4242-ui') || '{}'); } catch {}
const app = {poseReviewState:null,poseReviewStarted:0,poseReviewInFlight:null,poseNotice:null,poseReview:null,actionContext:null,preflighting:false,posePlan:null,poseError:null,poseSeq:0,waistPending:false,waistError:null,waistIntent:null,submittedDraft:null,startup:null,waistPreview:null,waistPreviewSeq:0,waistDelta:0,waistMode:'cartesian',wholePart:'arms',teleop:null,teleopOnline:false,teleopBusy:false,teleopPollBusy:false,page:'overview',group:stored.group==='base'?'base':'pose',draft:{},dirty:new Set(),viewer:null,viewerGeneration:0,data:null,dataBusy:false,recording:null,recordingOnline:false,recordingBusy:false,recordingPollBusy:false,selectedEpisode:null,episodeDetail:null,reviewDirty:false,presetName:stored.presetName || '',liftLevel:null,liftPercent:null,jogDeadlineTimer:null,robot:null,state:null,system:null,presets:[],events:[],lease:null,apiOnline:false,authenticated:false,lastReceived:0,activeAction:null,pendingAction:null,pendingHeadRecovery:null,revision:null,stateBusy:false,jog:null,jogTimer:null,jogPending:null,stopping:false};
function persist() { try { sessionStorage.setItem('f1-4242-ui',JSON.stringify({group:app.group,presetName:app.presetName})); } catch {} }
function toast(message, error = false) { const el = document.createElement('div');el.className = `toast${error ? ' error' : ''}`;el.textContent=message;$('#toast-region').append(el);setTimeout(()=>el.remove(),6500); }
function humanError(message){
 const match=String(message).match(/当前反馈超出已核验限位: (\w+)/);if(match){const name=match[1],value=app.state?.joints?.[name]?.position,range=app.robot?.joint_limits_deg?.[name];return `${labelJoint(name)}实测 ${fmt(value,1)}° 超出允许范围 ${range?range.join(' 至 '):'未知'}°，请先核对该部位；整套动作未开始。`;}
 return String(message).replace(/arm_[lr]_j[1-7]|head_j[1-3]|body_j[1-5]/g,labelJoint);
}
async function api(path, body, options = {}) {
  const controller = new AbortController(); const timeout = setTimeout(()=>controller.abort(), options.timeout || 5000);
  try {
    const response = await fetch(path,{method:body===undefined?'GET':'POST',credentials:'same-origin',cache:'no-store',headers:body===undefined?{}:{'Content-Type':'application/json','X-F1-Request':'1'},body:body===undefined?undefined:JSON.stringify(body),signal:controller.signal,keepalive:options.keepalive || false});
    const data = await response.json();
    if(!response.ok || data.ok===false) { if(response.status===401){app.authenticated=false;updateGlobal();} throw new Error(humanError(data.error || `服务返回 ${response.status}`)); }
    return data;
  } catch(error) { if(error.name==='AbortError')throw new Error('请求超时，请检查本地服务连接');throw error; }
  finally { clearTimeout(timeout); }
}
async function run(action) { try { await action(); } catch(error) { if(app.page==='control'){app.poseError=error.message;updatePoseSummary();}else toast(error.message,true); } }
function groups() { return app.robot?.groups || fallbackGroups; }
function groupNames(group=app.group) { const g=groups();if(group==='pose')return [...g.left_arm,...g.right_arm,...g.body,...g.head];return group==='whole'?groupNames(app.wholePart):group==='compound'?[...(g.body||[]),...(g.head||[])]:group==='lift'?(g.body||[]):group==='arms'?[...(g.left_arm||[]),...(g.right_arm||[])]:g[group]||[]; }
function fresh(entry, maxAge=.5) { return finite(entry?.position) && finite(entry?.age_s) && entry.age_s <= maxAge; }
function isFreshStream(entry) { return finite(entry?.age_s) && entry.age_s <= 2; }
function ownLease() { return Boolean(app.lease && app.apiOnline && app.authenticated); }
function canMove() { return ownLease() && Boolean(app.state?.connected); }
function actionRunning(action=app.activeAction){return Boolean(action&&['accepted','queued','running','stopping'].includes(action.status));}
function poseBusy(){return Boolean(app.preflighting||app.executing||app.pendingAction||actionRunning());}
function presetLabel(p){return p?.source==='native_pose_2'?'全身复位':p?.source==='lxl_xy_reset'?'双臂复位':p?.name||'姿态调整';}
function clearPresetSelection(){app.activePresetId=null;$$('.preset-item.selected').forEach(el=>el.classList.remove('selected'));}
function pendingTitle(){return app.activePresetId?presetLabel(app.presets.find(p=>p.id===app.activePresetId)):'姿态调整';}
function estimatedTime(plan){return plan?.estimated_total_s??plan?.motion_s;}
function verifiedAction(action){return action?.status==='succeeded'&&action.result?.feedback_verified===true;}
function headBoundaryPhase(action=app.activeAction){return (action?.progress_detail?.phase||action?.progress?.phase||action?.phase||action?.result?.phase)==='head_boundary_observation';}
function headBoundaryPlan(plan){
 const r=plan?.head_boundary_recovery;if(!r)return null;
 const bounds=app.robot?.joint_limits_deg?.head_j2;
 const direction=Array.isArray(bounds)&&finite(r.origin)?(r.origin<bounds[0]?1:r.origin>bounds[1]?-1:0):0;
 const edge=direction===1?bounds[0]:direction===-1?bounds[1]:null;
 if(r.joint!=='head_j2'||!Array.isArray(bounds)||bounds.length!==2||!bounds.every(finite)||!finite(r.origin)||!finite(r.edge)||!finite(r.outside_deg)||r.outside_deg<=0||!direction||r.edge!==edge||direction*(r.edge-r.origin)<=0||(r.direction!==undefined&&r.direction!==direction)||r.speed_controlled!==false||Math.abs(Math.abs(r.edge-r.origin)-r.outside_deg)>.01)throw new Error('头部回程计划不完整，请重新检查目标');
 return structuredClone(r);
}
function configureExecutionAuthorization(){
 const needed=!ownLease(),input=$('#execute-code');input.hidden=!needed;input.disabled=!needed;input.required=needed;
 $('label[for="execute-code"]').hidden=!needed;return needed;
}
function briefPoseError(message,started=false){const text=humanError(message);return /头部.*(?:超出|越界)/.test(text)?(started?'头部反馈异常，任务已中止。':'头部当前状态无法平滑复位，整套动作尚未开始。'):text.split('\n')[0];}
function labelJoint(name) { const match=/^arm_([lr])_j(\d+)$/.exec(name);if(match)return `${match[1]==='l'?'左臂':'右臂'} J${match[2]}`;if(name.startsWith('head_j'))return `头部 J${name.slice(6)}`;if(name.startsWith('body_j'))return `身体 J${name.slice(6)}`;return name; }
function pill(label,type='neutral') {return `<span class="pill ${type}">${esc(label)}</span>`;}
function heading(kicker,title,desc,actions='') {return `<div class="page-heading"><div><h1>${title}</h1>${desc?`<p>${desc}</p>`:''}</div>${actions?`<div class="heading-actions">${actions}</div>`:''}</div>`;}
function cardHeader(title,desc='',suffix='') {return `<div class="card-header"><div><h2>${title}</h2>${desc?`<p>${desc}</p>`:''}</div>${suffix}</div>`;}
function empty(title,desc,ic='eye') {return `<div class="empty-state compact">${icon(ic)}<h3>${title}</h3><p>${desc}</p></div>`;}
function leasePanel() {return `<section class="lease-card" id="lease-panel"><div><h3 id="lease-heading">观察模式</h3><p id="lease-description">输入控制码取得控制权，再预览并执行目标。</p></div><div class="lease-buttons"><button class="button primary" data-click="acquire" id="acquire-button">取得控制权 ${icon('arrow')}</button><button class="button" data-click="release" id="release-button" hidden>释放控制权</button></div></section>`;}
const cameraPanel=cameraHTML;
let cameraPreview=null;
let motionReplay=null;

function overviewPage() {
 return `${heading('','运行总览',esc(app.robot?.label||'F1'),`<button class="button" data-nav="system">系统配置</button><button class="button primary" data-nav="control">进入调姿 ${icon('arrow')}</button>`)}
 <section class="overview-health" aria-label="设备实时状态"><div class="health-grid" id="health-grid"></div></section>${cameraPanel()}
 <div class="grid overview-grid"><section class="card">${cardHeader('当前任务')}<div id="work-status" class="line-list"></div><div class="quick-links mt"><button class="button" data-nav="hil">HIL 采集 ${icon('arrow')}</button><button class="button" data-nav="pico">Pico 遥操 ${icon('arrow')}</button><button class="button" data-nav="data">数据回看 ${icon('arrow')}</button></div></section><section class="card">${cardHeader('最近操作')}<div class="event-list" id="event-list"></div></section></div>
 <details class="card overview-joints"><summary>双臂实测角度 <span>°</span></summary><div class="feedback-columns" id="joint-overview"></div></details>`;
}
function controlPage() {
 return `${heading('','调姿','')}${cameraPanel('实时视野','三路相机')}<div class="pose-control-status" id="pose-control-status" hidden><span>操控模式</span><div><button class="button small" data-click="stop">停止动作</button><button class="button small" data-click="release">退出操控</button></div></div><div class="control-layout"><section class="editor-card"><div class="pose-navigation" role="tablist">${[['pose','调姿'],['base','移动']].map(([key,label])=>`<button class="tab ${app.group===key?'active':''}" role="tab" aria-selected="${app.group===key}" data-group="${key}">${label}</button>`).join('')}</div><div id="capability-note" class="notice warn" hidden></div><div id="control-editor"></div></section><aside class="stack model-column"><section class="card pose-presets">${cardHeader('预设姿态', '', `<button class="button small" data-click="new-preset">${icon('plus')} 新增</button>`)}<div class="preset-list" id="preset-list"></div></section><section class="card model-card">${cardHeader('F1', '', `<button class="button small" data-click="focus-model">放大模型</button>`)}<div id="robot-model" class="robot-model"></div><p id="model-ground-warning" class="notice warn" hidden></p><div class="model-legend"><span><i></i>实测</span><span><i class="target"></i>目标</span></div><details class="model-info"><summary>模型说明</summary><p>姿态参考；地面检查不覆盖自碰撞、环境或平衡。拖动旋转，滚轮缩放。</p></details></section></aside></div>`;
}
function picoPage() {
 return `${heading('PICO TELEOPERATION','Pico 遥操','选择版本，检查现场，确认后启动。切换版本前先停止当前会话。')}${cameraPanel()}<section class="card" id="teleop-session"></section><div class="grid two mt"><section class="card">${cardHeader('meaxiao · 上肢版','1.4.4 基线 / F1-09 upper-v2',pill('双臂精细操作'))}<p>左右臂独立 Grip 接管，启动对齐实测姿态，保留精调、滤波与轨迹输出。</p><div class="notice mt">关闭底盘、腰部、ABXY 复位和手柄录制。数采在本页独立启动，保存后到「数据」页查看。</div><div class="quick-links mt"><button class="button primary" data-teleop="prepare" data-variant="upper">准备启动上肢版</button><button class="button" data-click="pico-recording">打开数采</button></div><details class="mt"><summary>现场终端脚本</summary><h3>检查 / 启动 / 停止</h3><pre>cd ${esc((app.state?.pico?.start_script||'integrations/pico_upper/start.sh').replace(/\/start\.sh$/,''))}
./check.sh
./start.sh
# 结束本会话
./stop.sh</pre><p class="section-note">原终端启动流程需先运行 PCService；网页准备会复用已有服务，或启动自己的服务。</p></details></section><section class="card">${cardHeader('LXL · 原始流程','参考文档 v5 / 0804 · 部署版本以实机为准',pill('main 保持原样'))}<p>调用原 start_all_pico1.sh，保留原始初始姿态、夹爪、腰部和 MCAP 数采行为。</p><div class="notice warn mt">启动可能移动到配置初始姿态；X / Y 仍会升降腰部。这套不是上肢限制模式。</div><div class="quick-links mt"><button class="button" data-teleop="prepare" data-variant="lxl">准备启动 LXL</button></div><details class="mt"><summary>现场终端脚本</summary><pre>cd /home/user/lxl
./start_all_pico1.sh
# 原停止脚本
./stop_all_pico.sh</pre><p class="section-note">原停止脚本按名称结束遥操和 PCService；网页停止改用本会话的进程组，不直接执行这个批量停止脚本。</p></details></section><section class="card recorder-card span-two" id="recorder-card">${empty('正在读取独立录制器','此入口使用独立 LeRobot 上肢分支。','data')}</section><section class="card span-two">${cardHeader('操作与数采','两套按键逻辑不同，使用前核对版本')}<div class="table-scroll"><table class="data-table"><thead><tr><th>功能</th><th>LXL 原流程</th><th>meaxiao 上肢适配</th></tr></thead><tbody><tr><td>手臂接管</td><td>扳机接管，松开停止；再次接管基于当前姿态</td><td>左右 Grip 独立接管；松开 Grip 后退出该臂跟随</td></tr><tr><td>夹爪</td><td>按文档：扳机按下张开，松开闭合</td><td>接管后由扳机控制夹爪；未接管不改夹爪</td></tr><tr><td>A / B</td><td>开始 / 停止原 MCAP 数采</td><td>关闭；本页使用独立上肢录制</td></tr><tr><td>X / Y / XY</td><td>腰下降 / 上升（超过 0.3 秒）；XY 回配置初始位</td><td>全部关闭，不执行全身复位</td></tr><tr><td>保存位置</td><td>/home/user/lxl/pico_data/all_data/</td><td>${esc(app.recording?.output_root||'由后端配置')}；本地保存</td></tr><tr><td>采样</td><td>文档 v5 标称 30 Hz / 13 类数据，需实测验证</td><td>当前独立录制为 16D / ${finite(app.recording?.fps)?app.recording.fps:'—'} FPS，三路相机</td></tr></tbody></table></div><p class="section-note">Pico 与 F1 接同一局域网。在 Xrobotokit 选择 F1，启用 Head、Controller、Hand，再点击 Send。原 LXL 文档仅说明双臂与静态身体的碰撞检测，不保证动态双臂之间的碰撞保护。</p></section></div>`;
}
function dataPage() {
 return `<div class="data-workspace">${heading('DATA LIBRARY','数据','',`<button class="button" data-click="refresh-data">${icon('refresh')} 刷新</button>`)}<div id="data-library-view"><section class="card data-library"><div class="data-library-heading"><div><h2>录制库</h2><span id="episode-count" class="muted">读取中…</span></div><div class="data-kind-tabs" role="group" aria-label="数据类型"><button data-data-kind="recordings" aria-pressed="${dataFilters.kind==='recordings'}">录制</button><button data-data-kind="exports" aria-pressed="${dataFilters.kind==='exports'}">训练导出</button></div></div><div class="data-filter-bar"><label class="data-search"><span>搜索录制</span><input id="data-search" type="search" value="${esc(dataFilters.query)}" placeholder="搜索名称、来源" autocomplete="off"></label><label class="data-review-filter"><span>审核</span><select id="data-review-filter">${[['all','全部状态'],['pending','待审核'],['approved','已通过'],['rejected','需排查']].map(([value,label])=>`<option value="${value}" ${dataFilters.review===value?'selected':''}>${label}</option>`).join('')}</select></label></div><div id="data-folders">${empty('正在读取录制','','data')}</div></section><details class="card data-storage-details"><summary>存储与录制状态</summary><div class="data-storage-expanded"><aside class="data-storage" id="data-storage" aria-label="本机数据存储概览"></aside><div id="data-status"></div></div></details></div><div id="episode-detail" hidden></div></div>`;
}
function startupPanel(){return `<section class="card startup-card"><div class="card-header"><div><h2>基础启动</h2><p>start_all.sh · 驱动与三路相机</p></div><button class="button primary" data-click="startup" id="base-start-button">启动基础服务</button></div><p id="base-start-status" class="notice">正在读取基础服务状态…</p><details class="mt"><summary>包含哪些程序 / 现场命令</summary><p class="section-note">原脚本包含运动学、电机、三相机、原升降、全身姿态与手柄节点。EC 主站独立启动；Pico、录制均独立。已存在节点时不重复执行。</p><pre>cd /home/user/workspace
./start_ec_master.sh
# EC 主站未运行时，在现场终端启动；不要重复运行。
# 另开终端，或使用网页明确确认：
./start_all.sh</pre></details></section>`;}
async function refreshStartup(){try{const r=await api('/api/startup/state');app.startup=r.startup;if($('#base-start-status'))$('#base-start-status').textContent=app.startup.reason||'基础节点未运行，可确认启动原脚本';if($('#base-start-button'))$('#base-start-button').disabled=!app.startup.can_start;}catch{if($('#base-start-status'))$('#base-start-status').textContent='基础启动状态暂不可用';if($('#base-start-button'))$('#base-start-button').disabled=true;}}
async function startBase(){if(!await login())return;if(!confirm('启动原 start_all.sh？将启动电机、运动学、三路相机和原升降/全身/手柄节点；不会启动 Pico 或数采。请在现场确认 EC 主站已就绪，手柄无人操作。'))return;const r=await api('/api/startup/start',{confirm:true});toast(r.startup.message);await refreshStartup();}
function systemPage() {
 return `${heading('','系统配置','',`<button class="button" data-click="refresh">刷新状态</button>`)}${startupPanel()}<div class="grid two system-grid"><section class="card">${cardHeader('机器人身份')}<dl class="system-definition" id="system-identity"></dl><div class="quick-links mt"><a id="vendor-link" class="button" href="http://192.168.31.58/" target="_blank" rel="noopener noreferrer">原厂运维 ${icon('arrow')}</a></div></section><section class="card">${cardHeader('访问与控制')}<div id="session-status" class="line-list"></div><div class="quick-links mt"><button class="button primary" data-click="login">启用操作</button><button class="button" data-click="logout">退出会话</button></div></section><section class="card span-two">${cardHeader('服务状态')}<div id="system-services" class="line-list"></div></section><details class="card span-two system-technical"><summary>版本与连接信息</summary><div class="line-list"><div class="line-item"><span class="label">访问地址</span><span class="value mono">${esc(location.origin)}</span></div><div class="line-item"><span class="label">资源版本</span><span class="value mono" id="revision-label">等待检测</span></div></div></details></div>`;
}
const hilPanel=new HILPanel({api,esc,toast,login});
function hilPage(){return `${heading('HUMAN IN THE LOOP','HIL','F710 遥操与人工纠正采集。')}${hilPanel.html()}`;}
const pages={hil:hilPage,overview:overviewPage,control:controlPage,pico:picoPage,data:dataPage,system:systemPage};
function renderPage() {
  let key=location.hash.slice(1).split('?')[0];
  if(['experiments','demos','demo'].includes(key)){key='control';app.group='pose';persist();history.replaceState(null,'','#control');}
  app.page=pages[key]?key:'overview';
  document.body.dataset.page=app.page;
  $('#navigation').innerHTML=tabs.map(([name,label,ic],i)=>`<button class="nav-item ${app.page===name?'active':''}" data-nav="${name}" aria-label="${esc(label)}" title="${esc(label)}" ${app.page===name?'aria-current="page"':''}>${icon(ic)}<span>${label}</span><span class="nav-index">0${i+1}</span></button>`).join('');
  $('#breadcrumb-title').textContent=tabs.find(row=>row[0]===app.page)[1];
  cameraPreview?.dispose();cameraPreview=null;hilPanel.dispose();disposeMediaViewers();app.viewer?.dispose();app.viewer=null;app.viewerGeneration++;$('#main').innerHTML=pages[app.page]();document.title=`${tabs.find(row=>row[0]===app.page)[1]} · F1 4242`;
  if(app.page==='control'){renderControlEditor();renderPresets();mountRobotViewer();}if(app.page==='data'){refreshData();const eid=new URLSearchParams(location.hash.split('?')[1]||'').get('episode');if(/^[a-f0-9]{24}$/.test(eid||'')&&app.selectedEpisode!==eid)selectEpisode(eid);}
  const preview=document.querySelector('[data-camera-preview]');if(preview)cameraPreview=new CameraPreview(preview,{fps:app.page==='hil'?10:4/3});
  if(app.page==='hil'){hilPanel.mount();hilPanel.refresh();}
  if(app.page==='pico'){renderRecording();refreshRecording();}
  updatePanels();updateGlobal();if(app.page==='system')refreshStartup();
}
function poseToolbar(){return `${app.replayReturn?`<a class="button small" href="#data?episode=${encodeURIComponent(app.replayReturn)}">返回 Replay 准备</a>`:''}<div class="pose-toolbar"><div class="pose-intent"><strong id="pose-scope">请选择目标</strong><p id="pose-plan-summary" aria-live="polite">调整姿态，或从右侧载入</p></div><div class="pose-toolbar-actions"><button class="button" data-click="align" title="放弃全部待执行目标">${icon('refresh')} 重新对齐</button><button class="button primary" data-click="execute-current" data-execute-required>执行 ${icon('arrow')}</button></div><div id="action-status" aria-live="polite"></div></div>`;}
const openPoseSections=new Set();
function renderControlEditor(){
 const holder=$('#control-editor');if(!holder)return;
 if(app.group==='base'){
   holder.innerHTML=`<div class="notice">按住移动，松手、切换页面或窗口失焦停止。连续按住最多 6 秒，需松手后再次按下。服务端保留短周期失联停止。</div><div class="base-pad"><button class="button" data-jog="turn-left" aria-label="按住左转">↶</button><button class="button" data-jog="forward" aria-label="按住前进">↑</button><button class="button" data-jog="turn-right" aria-label="按住右转">↷</button><button class="button" data-jog="left" aria-label="按住向左平移">←</button><span class="center">按住点动</span><button class="button" data-jog="right" aria-label="按住向右平移">→</button><span></span><button class="button" data-jog="back" aria-label="按住后退">↓</button></div><div class="base-meta"><span>线速度 0.05 m/s</span><span>角速度 0.10 rad/s</span></div><div class="line-list mt" id="base-feedback"></div>`;
   $$('[data-jog]',holder).forEach(button=>{
      button.addEventListener('pointerdown',event=>{event.preventDefault();if(event.button!==0)return;button.setPointerCapture?.(event.pointerId);startJog(button.dataset.jog,button);});
      for(const event of ['pointerup','pointercancel','lostpointercapture'])button.addEventListener(event,()=>stopJog());
      button.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();if(!event.repeat)startJog(button.dataset.jog,button);}});
      button.addEventListener('keyup',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();stopJog();}});
   });

 }else{
  function editor(name,label){const limits=app.robot?.joint_limits_deg?.[name];return `<div class="joint-editor" data-editor-row="${name}"><div class="joint-row-title"><label for="target-${name}">${label}</label><span class="live-measurement" title="当前实测"><output data-joint-live="${name}">—</output>°</span></div>${targetControl(name,limits,labelJoint(name),.1)}</div>`;}
  function grip(side){const name='gripper_'+side;return `<div class="joint-editor" data-editor-row="${name}"><div class="joint-row-title"><label for="target-${name}">夹爪</label><span class="live-measurement"><output data-gripper-live="${side}">—</output>%</span></div>${targetControl(name,[0,100],side==='left'?'左夹爪':'右夹爪',1)}</div>`;}
  holder.innerHTML=`<details class="pose-section" id="arms-section"><summary>双臂 <span>7 组关节 · 夹爪</span></summary><div class="arm-comparison-heading"><strong>左臂</strong><strong>右臂</strong></div><div class="arm-comparison">${Array.from({length:7},(_,i)=>`<div class="arm-pair">${editor('arm_l_j'+(i+1),'J'+(i+1))}${editor('arm_r_j'+(i+1),'J'+(i+1))}</div>`).join('')}<div class="arm-pair">${grip('left')}${grip('right')}</div></div></details><details class="pose-section" id="body-section"><summary>身体 <span>升降 · 转身 · 弯腰</span></summary><div class="body-height"><h3>升降</h3><div class="slider-field height-slider"><input type="range" min="-10" max="10" step=".1" value="0" data-waist-delta aria-label="模型相对升降滑块"></div><div class="height-control"><button class="button" data-waist-step="-2">下降 2 cm</button><label><input id="waist-delta" type="number" min="-10" max="10" step=".1" value="0" data-waist-delta aria-label="保持俯仰的模型相对高度"> cm</label><button class="button" data-waist-step="2">上升 2 cm</button></div><p id="waist-plan-status" class="section-note">保持当前弯腰角度 · 模型相对位移</p></div>${editor('body_j1','转身')}${editor('body_j2','侧倾')}${editor('body_j3','弯腰')}<details class="advanced-joints"><summary>高级关节调整</summary>${['body_j4','body_j5'].map((n,i)=>editor(n,'身体 J'+(i+4))).join('')}<p>修改关节角度会替代当前升降目标。</p></details></details><details class="pose-section" id="head-section"><summary>头部 <span>3 组关节</span></summary>${groups().head.map((n,i)=>editor(n,'J'+(i+1))).join('')}</details><details class="motion-info"><summary>控制说明</summary><p>选择目标后点击执行，系统协调所有待调整部位。保持固定速度，预计用时显示在顶部。保留关节行程与模型地面检查，尚无完整碰撞规划。</p></details>`;
 }
 $$('.pose-section',holder).forEach(section=>{section.open=openPoseSections.has(section.id);section.addEventListener('toggle',()=>{if(section.isConnected){if(section.open)openPoseSections.add(section.id);else openPoseSections.delete(section.id);}});});
 holder.insertAdjacentHTML('afterbegin',poseToolbar());updateControl();updateGlobal();updatePoseSummary();
}
let waistPreviewTimer;
function updateWaistPreview(){
 const note=$('#waist-plan-status');if(!note)return;
 $$('[data-waist-delta]').forEach(el=>{if(el!==document.activeElement)el.value=app.waistDelta;});
 note.textContent=app.waistPending?'正在计算保持俯仰的三轴路径…':app.waistError|| (app.waistPreview?`保持俯仰 · 相对 ${fmt(app.waistDelta,1)} cm · 预计 ${fmt(app.waistPreview.motion_s,1)} 秒`:'保持当前弯腰角度 · 模型相对位移');
}
function editWaistDelta(raw){
 if(poseBusy())return;clearPresetSelection();app.activeAction=null;
 const value=Number(raw);app.waistPending=true;app.waistError=null;app.waistDelta=value;app.waistIntent=null;app.waistPreview=null;app.waistPreviewSeq++;const seq=app.waistPreviewSeq;
 invalidatePose();updateWaistPreview();updateGlobal();clearTimeout(waistPreviewTimer);
 if(raw===''||!Number.isFinite(value)||Math.abs(value)>10){app.waistPending=false;app.waistError='请输入 -10 至 10 的模型相对位移';updateWaistPreview();invalidatePose();return;}
 waistPreviewTimer=setTimeout(async()=>{
  try{
   const result=await api(`/api/waist/preview?delta_m=${encodeURIComponent(value/100)}`,undefined,{timeout:15000});
   if(seq!==app.waistPreviewSeq)return;
   // The draft survives navigation; only newer edits invalidate this response.
   app.waistPreview=result.plan;app.waistIntent=value/100;
   for(const n of liftNames){app.draft[n]=result.plan.targets[n];app.dirty.add(n);}
  }catch(error){if(seq!==app.waistPreviewSeq)return;app.waistError=error.message;app.waistPreview=null;}
  finally{if(seq===app.waistPreviewSeq){app.waistPending=false;updateWaistPreview();invalidatePose();updateControl();updateGlobal();}}
 },250);
}
function measuredLiftPercent(){
 const positions=liftNames.map(name=>app.state?.joints?.[name]);
 if(!app.apiOnline||positions.some(entry=>!fresh(entry)))return null;
 try{return projectLift(positions.map(entry=>entry.position),app.robot?.joint_limits_deg);}catch{return null;}
}
function editLiftPercent(raw){
 const value=raw===''?NaN:Number(raw);
 if(!Number.isFinite(value)||value<0||value>100){app.liftPercent=null;app.liftLevel=null;updateGlobal();return;}
 app.liftPercent=value;app.liftLevel=null;
 let targets;try{targets=liftTarget(value,app.robot?.joint_limits_deg);}catch(error){app.liftPercent=null;updateGlobal();toast(error.message,true);return;}
 liftNames.forEach(name=>{app.draft[name]=targets[name];app.dirty.add(name);});
 updateControl();updateGlobal();
}
function syncLiftControl(){
 if(app.group!=='lift'||!$('#lift-slider'))return;
 const measured=measuredLiftPercent(),edited=app.liftPercent!==null,value=edited?app.liftPercent:measured;
 for(const input of $$('[data-lift-percent]')){
  input.disabled=measured===null;
  if(document.activeElement!==input)input.value=value===null?'':Number(value.toFixed(1));
  input.style.setProperty('--range-fill',`${value??0}%`);
 }
 $('#lift-target-label').textContent=edited?`目标 ${fmt(value,1)}%`:measured===null?'等待实测':'实测近似位置 · 尚未编辑';
 $('#lift-draft-angles').textContent=edited?`目标 J3 / J4 / J5：${liftNames.map(name=>fmt(Number(app.draft[name]),2)+'°').join(' / ')}`:'未编辑时保持当前实测姿态；拖动后三个升降关节联动。';
 try{const range=liftRange(app.robot?.joint_limits_deg);$('#lift-range-note').textContent=`J3 / J4 / J5 范围：${range.low.join(' / ')}° → ${range.high.join(' / ')}°；${labelJoint(range.limitingJoint)} 先到限位。`;
 $$('[data-lift-level]').forEach(button=>{let percent=null;try{percent=presetPercent(Number(button.dataset.liftLevel),app.robot?.joint_limits_deg);}catch{}button.disabled=percent===null;button.classList.toggle('active',edited&&percent!==null&&Math.abs(value-percent)<.01);});
 }catch{$('#lift-range-note').textContent='等待 F1 关节限位';$$('[data-lift-level]').forEach(button=>button.disabled=true);}
}
function targetControl(name,limits,label,step) {
 const valid=Array.isArray(limits)&&limits.length===2&&limits.every(finite),value=app.draft[name]??'';
 return `<div class="target-control"><div class="slider-field"><div class="range-limits"><span>${valid?esc(limits[0]):'—'}</span><span>${valid?esc(limits[1]):'—'}</span></div><input type="range" ${valid?`min="${limits[0]}" max="${limits[1]}"`:'disabled'} step="${step}" data-target-slider="${esc(name)}" aria-label="${esc(label)}目标滑块" ${value!==''?`value="${esc(value)}"`:'disabled'}></div><div class="number-field"><input id="target-${esc(name)}" type="number" ${valid?`min="${limits[0]}" max="${limits[1]}"`:''} step="any" data-target-input="${esc(name)}" aria-label="${esc(label)}目标" placeholder="等待实测" value="${esc(value)}"><span>${name.startsWith('gripper_')?'%':'°'}</span></div></div>`;
}
function seedLiveTargets() {
 for(const [name,entry] of Object.entries(app.state?.joints||{}))if(!app.dirty.has(name)&&fresh(entry))app.draft[name]=Math.round(entry.position*1000)/1000;
 for(const side of ['left','right']){const name=`gripper_${side}`,entry=app.state?.grippers?.[side];if(!app.dirty.has(name)&&fresh(entry))app.draft[name]=Math.round(entry.position*1000)/1000;}
}
function syncTargetInputs() {
 for(const el of $$('[data-target-input],[data-target-slider]')){
  const name=el.dataset.targetInput||el.dataset.targetSlider,value=app.draft[name];
  if(document.activeElement!==el)el.value=value??'';
  if(el.dataset.targetSlider){const limits=name.startsWith('gripper_')?[0,100]:app.robot?.limits?.[name]||app.robot?.joint_limits_deg?.[name];el.disabled=poseBusy()||!Array.isArray(limits)||value===undefined||value==='';if(Array.isArray(limits)&&finite(Number(value)))el.style.setProperty('--range-fill',`${Math.max(0,Math.min(100,(Number(value)-limits[0])/(limits[1]-limits[0])*100))}%`);}
 }
 $$('[data-editor-row]').forEach(el=>el.classList.toggle('edited',app.dirty.has(el.dataset.editorRow)));
}
async function mountRobotViewer(){
 const generation=app.viewerGeneration,container=$('#robot-model');if(!container)return;
 try{const module=await import('./model-viewer.js');if(generation!==app.viewerGeneration)return;container.replaceChildren();app.viewer=await module.createRobotViewer(container);if(generation!==app.viewerGeneration){app.viewer?.dispose();app.viewer=null;return;}updateRobotViewer();}catch(error){if(generation===app.viewerGeneration)container.innerHTML=`<div class="empty-state"><h3>模型暂不可用</h3><p>${esc(error.message)}</p></div>`;}
}
function updateRobotViewer(){if(!app.viewer)return;const entries=Object.entries(app.state?.joints||{});app.viewer.update({joints:Object.fromEntries(entries.filter(([,e])=>finite(e.position)).map(([name,e])=>[name,e.position])),draft:Object.fromEntries([...app.dirty].filter(name=>!name.startsWith('gripper_')&&app.draft[name]!==''&&finite(Number(app.draft[name]))).map(name=>[name,Number(app.draft[name])])),stale:!app.apiOnline||!entries.length||entries.some(([,e])=>!fresh(e))});}
function renderPresets(){
 const holder=$('#preset-list');if(!holder)return;
 holder.innerHTML=app.presets.length?app.presets.map((p,i)=>`<div class="preset-item ${app.activePresetId===p.id?'selected':''}"><button class="preset-load" data-preset-index="${i}">${esc(presetLabel(p))}</button><details class="preset-menu"><summary aria-label="姿态操作">···</summary><div><button class="button small" data-preset-export="${i}">导出 JSON</button><button class="button small" data-preset-delete="${i}">删除</button></div></details></div>`).join(''):empty('暂无姿态','点击新增保存当前姿态');
}
function statusFor(entry,type='feedback') {
 if(!app.apiOnline)return {text:'未连接',type:'neutral',desc:'本地服务不可达'};
 if(type==='service') {if(entry===true)return {text:'运行中',type:'good',desc:'服务已报告'};if(entry===false)return {text:'未运行',type:'neutral',desc:'未检测到服务'};if(typeof entry==='object'&&entry){const status=entry.status||entry.state;const healthy=entry.running===true||entry.ok===true||['running','ready','ok','active'].includes(status);return {text:entry.label||(typeof status==='string'&&status.length>12?'现场启动':status)||(healthy?'运行中':'待检查'),type:healthy?'good':'neutral',desc:entry.reason||entry.error||entry.message||'服务已报告'};}return {text:'待核实',type:'neutral',desc:'暂无运行证据'};}
 if(isFreshStream(entry))return {text:'反馈在线',type:'good',desc:`${fmt(entry.age_s,2)} 秒前`};
 if(finite(entry?.age_s))return {text:'反馈过期',type:'warn',desc:`${fmt(entry.age_s,1)} 秒前`};
 return {text:'等待反馈',type:'neutral',desc:'尚未收到有效样本'};
}
function groupStatus(names){const entries=names.map(name=>app.state?.joints?.[name]);const valid=entries.filter(e=>finite(e?.age_s));if(!valid.length)return null;return {age_s:valid.length===names.length?Math.max(...valid.map(e=>e.age_s)):Infinity};}
function updateOverview() {
 if(app.page!=='overview')return;
 const s=app.state||{},g=groups();
 const health=[['网页网关','signal',{text:app.apiOnline?'后端在线':'未连接',type:app.apiOnline?'good':'neutral',desc:s.connected?'ROS 订阅已启动，设备反馈见各项':'等待 ROS 订阅'}],['左臂','arm',statusFor(groupStatus(g.left_arm||[]))],['右臂','arm',statusFor(groupStatus(g.right_arm||[]))],['头部 / 身体','control',statusFor(groupStatus([...(g.head||[]),...(g.body||[])]))],['头部相机','camera',statusFor(s.cameras?.head)],['左腕相机','camera',statusFor(s.cameras?.left)],['右腕相机','camera',statusFor(s.cameras?.right)],['底盘','overview',statusFor(s.base)],['Pico','pico',statusFor(s.pico,'service')]];
 $('#health-grid').innerHTML=health.map(([label,ic,status])=>`<div class="health-item"><div class="health-top"><span>${label}</span>${icon(ic)}</div>${pill(status.text,status.type)}<p>${esc(status.desc)}</p></div>`).join('');
 $('#joint-overview').innerHTML=[['left_arm','左臂'],['right_arm','右臂']].map(([key,label])=>`<div><div class="feedback-column-header"><strong>${label}</strong><span>实测 · °</span></div>${(g[key]||[]).map((name,index)=>{const entry=s.joints?.[name],value=entry?.position,width=finite(value)?Math.min(Math.abs(value)/180*50,50):0;return `<div class="joint-mini ${entry&&!fresh(entry)?'stale':''}"><span>J${index+1}</span><div class="meter"><div class="meter-fill" style="left:${finite(value)&&value<0?50-width:50}%;width:${width}%"></div></div><output>${fmt(value)}${finite(value)?'°':''}</output></div>`;}).join('')}</div>`).join('');
 const active=s.action||app.activeAction;
 $('#work-status').innerHTML=lineList([['控制者',app.lease?'当前浏览器':s.control?.owner||'无已知控制者'],['操作会话',app.authenticated?'已启用':'只读观察'],['当前动作',active?actionLabel(active.status):'空闲']]);
 renderEvents($('#event-list'));
}
function lineList(rows){return rows.map(([label,value])=>`<div class="line-item"><span class="label">${esc(label)}</span><span class="value">${esc(value??'—')}</span></div>`).join('');}

function updateControl() {
 if(app.page!=='control')return;
 const s=app.state||{};syncTargetInputs();syncLiftControl();updateWaistPreview();updateRobotViewer();
 $$('[data-joint-live]').forEach(el=>{const entry=s.joints?.[el.dataset.jointLive];el.textContent=fmt(entry?.position);el.classList.toggle('stale',!app.apiOnline||!fresh(entry));el.title=finite(entry?.age_s)?`接收时间：${fmt(entry.age_s,2)} 秒前`:'尚未收到反馈';});
 $$('[data-gripper-live]').forEach(el=>{const entry=s.grippers?.[el.dataset.gripperLive];el.textContent=fmt(entry?.position);el.classList.toggle('stale',!app.apiOnline||!fresh(entry));});
 if($('#base-feedback'))$('#base-feedback').innerHTML=lineList([['反馈时效',finite(s.base?.age_s)?`${fmt(s.base.age_s,2)} 秒前`:'未收到'],['实测平移速度',`${fmt(s.base?.vx,3)} / ${fmt(s.base?.vy,3)} m/s`],['实测转速',`${fmt(s.base?.wz,3)} rad/s`],['局部位置',`${fmt(s.base?.pose?.x,3)} / ${fmt(s.base?.pose?.y,3)} m`]]);

 updatePoseSummary();

}
function actionLabel(status){return ({queued:'等待执行',running:'执行中',completed:'命令执行结束',succeeded:'执行结束',failed:'执行失败',rejected:'执行被拒绝',cancelled:'已取消',stopped:'已停止',stopping:'正在停止',accepted:'已受理'})[status]||status||'等待状态';}
const teleopLabels={idle:'尚未启动',starting:'正在启动',preparing:'正在预检',awaiting_confirmation:'等待现场确认',waiting_xr:'等待 Pico 数据与手柄松开',workers_starting:'正在等待遥操就绪',running:'遥操进程就绪',stopping:'正在停止',stopped:'已停止',failed:'启动失败或会话失联'};
function updatePico() {
 const t=app.teleop,active=t&&!['idle','stopped','failed'].includes(t.phase),label=teleopLabels[t?.phase]||'等待状态';
 const indicator=$('#teleop-indicator');if(indicator){indicator.hidden=!active&&!t?.can_stop;indicator.textContent=`● ${app.teleopOnline?label:'遥操状态失联'}`;}
 if(app.page!=='pico')return;
 const holder=$('#teleop-session');if(!holder)return;
 const services=app.state?.services||{},external=['lxl_teleop','lxl_bridge','pico_upper'].filter(k=>services[k]?.running);
 const blockers=!active?['joint_command','left_gripper_command','right_gripper_command'].flatMap(k=>(app.state?.graph?.[k]?.publishers||[]).map(p=>p.name||'unknown')):[];
 const {checked_unix,log_tail,...viewState}=t||{};
 const signature=JSON.stringify({t:viewState,online:app.teleopOnline,auth:app.authenticated,busy:app.teleopBusy,lease:app.lease,external,blockers});
 if(holder.dataset.signature!==signature){holder.dataset.signature=signature;
 const config=t?.legacy_config||{},poses=['target_left','target_right','reset_left','reset_right'].map(k=>`<div class="line-item"><span>${({target_left:'启动左臂',target_right:'启动右臂',reset_left:'复位左臂',reset_right:'复位右臂'})[k]}</span><span class="mono">${esc(Array.isArray(config[k])?config[k].join(', ')+' °':'未读到配置')}</span></div>`).join('');
 holder.innerHTML=`${cardHeader('网页遥操会话',t?.variant==='lxl'?'LXL 原始流程':t?.variant==='upper'?'meaxiao 上肢版':'一次运行一套遥操',pill(app.teleopOnline?label:'状态连接中',t?.phase==='running'?'good':t?.phase==='failed'?'warn':'neutral'))}${!app.authenticated?'<button class="button" data-click="login">输入控制码启用遥操操作</button>':''}${app.lease?'<div class="notice warn mt">先释放姿态控制权，再准备遥操。<button class="button small" data-click="release">释放控制权</button></div>':''}${!active&&external.length?'<div class="notice warn mt">检测到现场启动的遥操进程。请从原启动入口停止后再用网页；网页不会接管或结束外部会话。</div>':''}${blockers.length?`<div class="notice warn mt">Pico 原生预检仍要求独占：已有命令发布者 ${esc([...new Set(blockers)].join('、'))}。请从它们的原入口结束控制后重试。</div>`:''}${t?.error?`<div class="notice warn mt" role="alert">${esc(t.error)}</div>`:''}${t?.phase==='awaiting_confirmation'?`<div class="teleop-confirm mt"><h3>确认启动${t.variant==='lxl'?' LXL':'上肢遥操'}</h3><p>${t.variant==='lxl'?'LXL 将按下列配置初始化双臂，并保留腰部按键与原数采行为。请确认运动空间。':'请在机器人旁确认操作空间，松开两侧 Grip、扳机并保持摇杆居中。确认后开始等待 Pico 数据，再启动双臂与夹爪遥操。'}</p>${t.variant==='lxl'?`<div class="line-list">${poses}</div>`:''}<p class="section-note">确认有效期 120 秒。关闭页面不会停止已启动的遥操。</p><button class="button primary" data-teleop="confirm" ${!t.can_confirm||!app.authenticated||app.teleopBusy?'disabled':''}>我已在现场，确认启动</button>${!t.can_confirm?'<p>请由发起准备的浏览器完成确认；已提交时等待状态更新。</p>':''}</div>`:''}<div class="quick-links mt">${active||t?.can_stop?`<button class="button stop-button" data-teleop="stop" ${!app.authenticated||app.teleopBusy?'disabled':''}>${['preparing','awaiting_confirmation','starting'].includes(t.phase)?'取消本次启动':'停止本会话遥操'}</button>`:''}</div><p class="section-note">网页停止仅结束本会话的遥操及其专属 PCService，不代替硬件急停。刷新或关闭网页不会停止遥操；建议先结束录制，再停止遥操。</p><details class="mt"><summary>会话日志与 LXL 当前配置</summary><pre class="teleop-log" id="teleop-log">${esc(t?.log_tail||'暂无会话日志')}</pre><div class="line-list">${lineList([['LXL 局域网 IP',config.local_ip||'未读到配置']])}${poses}</div></details>`;
 }
 const log=$('#teleop-log');if(log&&log.textContent!==(t?.log_tail||'暂无会话日志'))log.textContent=t?.log_tail||'暂无会话日志';
 $$('[data-teleop="prepare"]').forEach(el=>el.disabled=!app.authenticated||!app.teleopOnline||!t?.available||active||t?.can_stop||app.teleopBusy||Boolean(app.lease));
}
async function refreshTeleop(){
 if(app.teleopPollBusy)return;app.teleopPollBusy=true;
 try{const result=await api('/api/teleop/state');app.teleop=result.teleop;app.teleopOnline=true;}catch{app.teleopOnline=false;}
 finally{app.teleopPollBusy=false;updatePico();}
}
async function teleopAction(operation,variant){
 if(app.teleopBusy)return;if(!app.authenticated){await login();return;}
 const t=app.teleop;app.teleopBusy=true;updatePico();
 try{const body=operation==='prepare'?{variant}:{session_id:t?.id,...(operation==='confirm'?{nonce:t?.nonce}:{})};const result=await api('/api/teleop/'+operation,body,{timeout:12000});app.teleop=result.teleop;app.teleopOnline=true;toast(operation==='prepare'?'正在预检，通过后请明确确认启动':operation==='confirm'?'启动确认已提交':'停止请求已提交');}
 finally{app.teleopBusy=false;await refreshTeleop();}
}
function recordingModeLabel(mode){return {not_started:'尚未启动',starting:'正在启动',idle:'准备就绪',recording:'正在录制',finalizing:'正在整理片段',review:'等待审核保存',error:'录制异常',unavailable:'暂不可用'}[mode]||'等待状态';}
function recordingDuration(value){if(!finite(value))return '—';const seconds=Math.max(0,Math.floor(value));return `${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;}
const activeMediaPlayers=new Set();
function episodeVideos(videos,options={}){
 const available=Array.isArray(videos)?videos.filter(v=>['head','left','right'].includes(v.camera)&&typeof v.url==='string'&&(v.url.startsWith('/api/data/')||v.url.startsWith('/api/recording/'))):[],analytics=typeof options.analytics_url==='string'&&options.analytics_url.startsWith('/api/data/')?options.analytics_url:'';
 if(options.format==='official_lerobot_v3')return `<div class="notice mt">官方 LeRobot v3 · ${esc(options.dataset_episodes)} 个片段 · ${options.training_ready?'已审核导出':'候选数据，待片段审核'}。这是训练文件集；完整操作回放请打开原始采集文件夹。</div>`;
 if(!available.length&&!analytics)return `<div class="notice mt">这条录制的视频与曲线缓存尚未就绪。原始数据已保留，刷新目录后可重新打开。</div>`;
 return `<section class="recording-video-section synced-review" data-media-viewer data-duration="${finite(options.duration_s)?options.duration_s:0}" data-analytics="${esc(analytics)}"><div class="recording-video-heading"><h3>${options.pending?'本条待保存片段':'同步视频回放'}</h3><span class="historical-label">历史片段 · 不控制机器人</span></div><div class="recording-video-grid">${['left','head','right'].map(camera=>{const video=available.find(v=>v.camera===camera),label={head:'头部 · 主视角',left:'左腕相机',right:'右腕相机'}[camera];return `<figure class="recorded-camera" data-review-camera="${camera}"><div class="recorded-frame">${video?`<video preload="metadata" playsinline muted src="${esc(video.url)}" data-camera="${camera}" data-offset="${finite(video.offset_s)?video.offset_s:0}" aria-label="${label}历史片段"></video><span class="video-state" data-video-state="${camera}">加载中</span>`:`<div class="recorded-empty">${icon('camera')}<span>该路暂无视频</span></div>`}</div><figcaption>${label}</figcaption></figure>`;}).join('')}</div><div class="shared-timeline"><div class="playback-controls"><button type="button" class="button primary" data-media-play aria-label="播放三路录制">播放</button><button type="button" class="button small" data-media-skip="-5" aria-label="后退五秒">−5 秒</button><button type="button" class="button small" data-media-skip="5" aria-label="前进五秒">+5 秒</button><output data-media-time>00:00.0 / —</output><label class="speed-control">速度<select data-media-speed aria-label="三路统一播放速度">${[0.25,0.5,1,1.5,2].map(speed=>`<option value="${speed}" ${speed===1?'selected':''}>${speed}×</option>`).join('')}</select></label></div><input type="range" min="0" max="1" value="0" step="0.01" data-media-seek aria-label="三路视频与曲线共用时间轴"><p data-media-status class="media-status">载入视频后使用共用时间轴；点击曲线也可定位。</p></div><section class="curve-panel"><div class="curve-toolbar"><div><h3>关节状态与动作</h3><p>与三路画面使用同一片段时间轴</p></div><label>查看通道<select data-curve-channel aria-label="曲线关节通道" disabled><option>等待曲线数据</option></select></label></div><div data-curve-loading class="curve-loading">${analytics?'正在读取曲线…':'此片段尚未提供曲线缓存。'}</div><div data-curve-content hidden><div class="curve-legend"><span><i class="state-line"></i>实测 state <output data-state-value>—</output></span><span><i class="action-line"></i>命令 action <output data-action-value>—</output></span><span data-curve-unit></span></div><canvas data-curve-canvas height="240" role="img" aria-label="选中关节的实测与动作曲线，点击或拖动定位对应视频时间" tabindex="0"></canvas><div class="curve-meta"><span data-curve-sample></span><span>点击 / 拖动曲线定位</span></div><details class="curve-notes"><summary>时间对齐与数据说明</summary><div data-curve-notes></div></details></div></section></section>`;
}
function disposeMediaViewers(scope){if(motionReplay&&(!scope||scope.contains(motionReplay.root))){motionReplay.dispose();motionReplay=null;}for(const player of [...activeMediaPlayers])if(!scope||scope.contains(player.root)){player.dispose();activeMediaPlayers.delete(player);}}
function mountMediaViewers(scope){for(const root of $$('[data-media-viewer]',scope)){if([...activeMediaPlayers].some(player=>player.root===root))continue;activeMediaPlayers.add(new SyncedEpisodePlayer(root));}}
class SyncedEpisodePlayer{
 constructor(root){this.root=root;this.videos=$$('video',root);this.time=0;this.speed=1;this.duration=Number(root.dataset.duration)||0;this.playing=false;this.disposed=false;this.channel=0;this.analytics=null;this.lastTick=0;this.lastPaint=0;this.pendingPlay=new Set();this.events=[];
  this.on($('[data-media-play]',root),'click',()=>this.toggle());this.on($('[data-media-seek]',root),'input',event=>this.seek(Number(event.target.value)));this.on($('[data-media-speed]',root),'change',event=>{this.speed=Number(event.target.value);this.videos.forEach(video=>video.playbackRate=this.speed);});
  for(const button of $$('[data-media-skip]',root))this.on(button,'click',()=>this.seek(this.time+Number(button.dataset.mediaSkip)));
  this.on($('[data-curve-channel]',root),'change',event=>{this.channel=Number(event.target.value);this.paint();});
  const canvas=$('[data-curve-canvas]',root);let dragging=false;const curveSeek=event=>{const rect=canvas.getBoundingClientRect(),width=rect.width-76;this.seek((event.clientX-rect.left-54)/Math.max(1,width)*this.duration);};this.on(canvas,'pointerdown',event=>{if(event.button!==0)return;dragging=true;canvas.setPointerCapture?.(event.pointerId);curveSeek(event);});this.on(canvas,'pointermove',event=>{if(dragging)curveSeek(event);});for(const event of ['pointerup','pointercancel','lostpointercapture'])this.on(canvas,event,()=>dragging=false);this.on(canvas,'keydown',event=>{if(event.key==='ArrowLeft'||event.key==='ArrowRight'){event.preventDefault();this.seek(this.time+(event.key==='ArrowRight'?1:-1));}});
  for(const video of this.videos){video.muted=true;this.on(video,'loadedmetadata',()=>{if(video.videoWidth&&video.videoHeight)video.parentElement.style.aspectRatio=`${video.videoWidth} / ${video.videoHeight}`;const end=video.duration+Number(video.dataset.offset||0);if(!Number(root.dataset.duration)&&finite(end))this.duration=Math.max(this.duration,end);this.sync(true);this.paint();});this.on(video,'loadeddata',()=>this.paint());this.on(video,'error',()=>{const label=$(`[data-video-state="${video.dataset.camera}"]`,root);if(label){label.textContent='此路视频暂不可用';label.hidden=false;}this.paint();});}
  this.on(document,'visibilitychange',()=>{if(document.hidden){this.playing=false;this.videos.forEach(v=>v.pause());this.paint();}});
  this.intersection=new IntersectionObserver(es=>{if(!es[0].isIntersecting){this.playing=false;this.videos.forEach(v=>v.pause());this.paint();}});this.intersection.observe(root);
  this.resizeObserver=new ResizeObserver(()=>this.paint());this.resizeObserver.observe(root);if(root.dataset.analytics)this.loadAnalytics(root.dataset.analytics);this.paint();this.raf=requestAnimationFrame(stamp=>this.tick(stamp));
 }
 on(target,type,handler){target?.addEventListener(type,handler);this.events.push([target,type,handler]);}
 async loadAnalytics(url){try{const response=await api(url,undefined,{timeout:20000});if(this.disposed)return;const data=response.analytics||response;if(!Array.isArray(data.timestamps_s)||!data.timestamps_s.length||data.timestamps_s.some((t,i,a)=>!finite(t)||(i>0&&t<a[i-1])))throw new Error('曲线时间戳缺失或顺序无效');this.analytics=data;const dimension=data.channel_names?.length||data.state?.find(Array.isArray)?.length||data.action?.find(Array.isArray)?.length||0;if(!dimension)throw new Error('曲线没有可显示的关节通道');const selector=$('[data-curve-channel]',this.root);selector.innerHTML=Array.from({length:dimension},(_,i)=>{const name=data.channel_names?.[i]||`通道 ${i+1}`,label=name==='gripper_left'?'左夹爪':name==='gripper_right'?'右夹爪':labelJoint(name);return `<option value="${i}">${esc(label)}</option>`;}).join('');selector.disabled=false;const end=finite(data.duration_s)?data.duration_s:data.timestamps_s.at(-1);if(end>0)this.duration=end;const deltas=data.timestamps_s.slice(1).map((t,i)=>t-data.timestamps_s[i]).filter(t=>t>0).sort((a,b)=>a-b);this.gapLimit=deltas.length?deltas[Math.floor(deltas.length/2)]*4:Infinity;$('[data-curve-loading]',this.root).hidden=true;$('[data-curve-content]',this.root).hidden=false;const notes=[...(Array.isArray(data.notes)?data.notes:[]),'曲线经过抽样显示；缺失值保留为空，不填零。',data.action?'action 显示采集时实际记录的命令，不代表当前机器人目标。':'此片段未提供 action 命令，仅显示可用的 state 实测。'];$('[data-curve-notes]',this.root).innerHTML=notes.map(note=>`<p>${esc(typeof note==='string'?note:JSON.stringify(note))}</p>`).join('');this.paint();}catch(error){if(!this.disposed)$('[data-curve-loading]',this.root).textContent=`曲线暂不可用：${error.message}`;}}
 playable(video){const local=this.time-Number(video.dataset.offset||0);return !video.error&&local>=0&&(!finite(video.duration)||local<video.duration-.02);}
 startVideo(video){if(!video.paused||this.pendingPlay.has(video)||!this.playable(video))return;this.pendingPlay.add(video);video.playbackRate=this.speed;const result=video.play();if(result?.catch)result.catch(error=>{if(error.name==='NotAllowedError'){this.playing=false;this.videos.forEach(v=>v.pause());$('[data-media-status]',this.root).textContent='浏览器未允许播放，请再次点击播放。';}}).finally(()=>this.pendingPlay.delete(video));else this.pendingPlay.delete(video);}
 toggle(){if(!this.duration)return;if(this.time>=this.duration-.02)this.seek(0);this.playing=!this.playing;if(this.playing)this.videos.forEach(video=>this.startVideo(video));else this.videos.forEach(video=>video.pause());this.paint();}
 seek(value){this.time=Math.max(0,Math.min(this.duration,Number.isFinite(value)?value:0));this.sync(true);this.paint();}
 sync(force=false){for(const video of this.videos){if(video.readyState<1||video.error)continue;const offset=Number(video.dataset.offset||0),local=Math.max(0,Math.min(Math.max(0,video.duration-.015),this.time-offset));if(force||Math.abs(video.currentTime-local)>.16){try{video.currentTime=local;}catch{}}if(this.playing&&this.playable(video))this.startVideo(video);else video.pause();}}
 tick(stamp){if(this.disposed)return;const delta=this.lastTick?Math.min(.12,(stamp-this.lastTick)/1000):0;this.lastTick=stamp;if(this.playing){const active=this.videos.filter(video=>this.playable(video)),buffering=active.some(video=>video.readyState<2||video.seeking);if(!buffering)this.time=Math.min(this.duration,this.time+delta*this.speed);if(buffering)this.videos.forEach(v=>v.pause());else this.sync();if(this.time>=this.duration){this.playing=false;this.videos.forEach(video=>video.pause());}if(stamp-this.lastPaint>65){$('[data-media-status]',this.root).textContent=buffering?'正在缓冲，等待各路图像对齐…':'三路视频与曲线同步播放';this.paint();this.lastPaint=stamp;}}this.raf=requestAnimationFrame(next=>this.tick(next));}
 stamp(value){if(!finite(value))return '—';const n=Math.max(0,value);return `${String(Math.floor(n/60)).padStart(2,'0')}:${(n%60).toFixed(1).padStart(4,'0')}`;}
 paint(){if(this.disposed)return;const root=this.root,play=$('[data-media-play]',root),seek=$('[data-media-seek]',root);play.textContent=this.playing?'暂停':'播放';play.disabled=!this.duration;play.setAttribute('aria-label',this.playing?'暂停三路录制':'播放三路录制');seek.max=this.duration||1;seek.value=this.time;seek.disabled=!this.duration;$('[data-media-time]',root).textContent=`${this.stamp(this.time)} / ${this.duration?this.stamp(this.duration):'—'}`;for(const video of this.videos){const label=$(`[data-video-state="${video.dataset.camera}"]`,root);if(!label||video.error)continue;const offset=Number(video.dataset.offset||0);label.hidden=video.readyState>=2&&this.time>=offset&&(!finite(video.duration)||this.time<offset+video.duration);if(!label.hidden)label.textContent=this.time<offset?'尚未到该路起点':finite(video.duration)&&this.time>=offset+video.duration?'该路已结束':'加载中';}this.paintCurve();}
 paintCurve(){const data=this.analytics;if(!data)return;const canvas=$('[data-curve-canvas]',this.root),rect=canvas.getBoundingClientRect();if(rect.width<20)return;const width=rect.width,height=240,dpr=Math.min(window.devicePixelRatio||1,2);if(canvas.width!==Math.round(width*dpr)||canvas.height!==height*dpr){canvas.width=Math.round(width*dpr);canvas.height=height*dpr;}const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,width,height);ctx.fillStyle='#fcfaf5';ctx.fillRect(0,0,width,height);const times=data.timestamps_s,channel=this.channel,series=[{rows:data.state,color:'#3d5d70'},{rows:data.action,color:'#bd7858'}],values=series.flatMap(s=>Array.isArray(s.rows)?s.rows.map(row=>row?.[channel]).filter(finite):[]),left=54,right=width-22,top=18,bottom=height-33;if(!values.length){ctx.fillStyle='#84796a';ctx.font='14px sans-serif';ctx.fillText('此通道没有有效值',left,80);return;}let min=Math.min(...values),max=Math.max(...values),pad=Math.max((max-min)*.12,.5);min-=pad;max+=pad;const x=time=>left+time/Math.max(this.duration,.001)*(right-left),y=value=>bottom-(value-min)/(max-min)*(bottom-top);ctx.font='11px sans-serif';ctx.lineWidth=1;for(let i=0;i<=4;i++){const yy=top+(bottom-top)*i/4;ctx.strokeStyle='#e8e1d5';ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(right,yy);ctx.stroke();ctx.fillStyle='#958a79';ctx.textAlign='right';ctx.fillText((max-(max-min)*i/4).toFixed(1),left-9,yy+4);}for(let i=0;i<=4;i++){const xx=left+(right-left)*i/4;ctx.fillStyle='#958a79';ctx.textAlign=i===0?'left':i===4?'right':'center';ctx.fillText(this.stamp(this.duration*i/4),xx,height-10);}ctx.save();ctx.beginPath();ctx.rect(left,top,right-left,bottom-top);ctx.clip();for(const seriesEntry of series){if(!Array.isArray(seriesEntry.rows))continue;ctx.strokeStyle=seriesEntry.color;ctx.lineWidth=1.7;ctx.beginPath();let connected=false,lastTime=null;for(let i=0;i<times.length;i++){const value=seriesEntry.rows[i]?.[channel];if(!finite(value)){connected=false;continue;}const xx=x(times[i]),yy=y(value);if(connected&&lastTime!==null&&times[i]-lastTime<=this.gapLimit)ctx.lineTo(xx,yy);else ctx.moveTo(xx,yy);connected=true;lastTime=times[i];}ctx.stroke();}const cursor=x(this.time);ctx.strokeStyle='#474038';ctx.lineWidth=1;ctx.setLineDash([4,3]);ctx.beginPath();ctx.moveTo(cursor,top);ctx.lineTo(cursor,bottom);ctx.stroke();ctx.setLineDash([]);let lo=0,hi=times.length-1;while(lo<hi){const mid=Math.ceil((lo+hi)/2);if(times[mid]<=this.time)lo=mid;else hi=mid-1;}const unit=data.units?.[channel]||'',unitLabel=unit==='deg'?'°':['percent','%'].includes(unit)?'%':unit;for(const [index,seriesEntry] of series.entries()){const value=seriesEntry.rows?.[lo]?.[channel];$(`[data-${index===0?'state':'action'}-value]`,this.root).textContent=finite(value)?`${fmt(value,2)}${unitLabel}`:'—';if(finite(value)){ctx.fillStyle=seriesEntry.color;ctx.beginPath();ctx.arc(x(times[lo]),y(value),3,0,Math.PI*2);ctx.fill();}}ctx.restore();$('[data-curve-unit]',this.root).textContent=unitLabel?`单位 ${unitLabel}`:'';$('[data-curve-sample]',this.root).textContent=`${times.length.toLocaleString()} 个显示采样点${finite(data.source_frame_count)?` / ${data.source_frame_count.toLocaleString()} 帧`:''}`;}
 dispose(){this.disposed=true;cancelAnimationFrame(this.raf);this.resizeObserver.disconnect();this.intersection.disconnect();this.videos.forEach(video=>{video.pause();video.removeAttribute('src');video.load();});this.events.forEach(([target,type,handler])=>target?.removeEventListener(type,handler));}
}

function renderRecording(){
 const r=app.recording,indicator=$('#recording-indicator'),mode=r?.mode,ongoing=['starting','recording','finalizing','review','error'].includes(mode);
 if(indicator){indicator.hidden=!ongoing;indicator.classList.toggle('is-recording',mode==='recording');indicator.classList.toggle('is-review',mode==='review');indicator.textContent=mode==='recording'?`● 录制中 ${recordingDuration(r.elapsed_s)}`:mode==='review'?'● 录制待保存':`● ${recordingModeLabel(mode)}`;if(!app.recordingOnline&&ongoing)indicator.textContent='● 录制状态失联';}
 const holder=$('#recorder-card');if(!holder)return;
 if(!r){holder.innerHTML=`${cardHeader('LeRobot · 独立上肢录制','原 LXL 采集保持现状')}${empty('录制服务未连接','等待独立录制器状态。','data')}`;return;}
 const signature=JSON.stringify({mode:r.mode,available:r.available,ready:r.ready,reasons:r.ready_reasons,output_root:r.output_root,task:r.task,frames:r.frames,elapsed_s:r.mode==='recording'?r.elapsed_s:null,review:r.review,last_saved:r.last_saved,last_error:r.last_error,online:app.recordingOnline,authenticated:app.authenticated,busy:app.recordingBusy});if(holder.dataset.viewSignature===signature)return;holder.dataset.viewSignature=signature;
 const reasons=Array.isArray(r.ready_reasons)?r.ready_reasons:[],canStart=app.recordingOnline&&r.available&&(mode==='not_started'||(mode==='idle'&&r.ready))&&!app.recordingBusy,canStop=app.recordingOnline&&mode==='recording'&&!app.recordingBusy,canSave=app.recordingOnline&&mode==='review'&&!app.recordingBusy;
 disposeMediaViewers(holder);
 const stateType=mode==='recording'?'bad':mode==='error'||mode==='unavailable'?'warn':mode==='review'?'warn':r.ready?'good':'neutral';
 holder.innerHTML=`${cardHeader('LeRobot · 独立上肢录制',`双臂与夹爪 · 16D / ${finite(r.fps)?r.fps:'—'} FPS`,pill(app.recordingOnline?(mode==='idle'&&!r.ready?'等待采集条件':recordingModeLabel(mode)):'状态失联',stateType))}<div class="recorder-layout"><div class="recorder-info"><label class="field-label">任务名称</label><div class="read-only-field">${esc(r.task||'F1 upper-body bimanual teleoperation')}</div><label class="field-label mt">保存位置</label><div class="read-only-field recorder-path">${esc(r.output_root||'等待目录配置')}</div><p class="section-note">开始采集不启动 Pico 或机器人动作。切换页面不会结束录制。</p></div><div class="recorder-session"><div class="recording-metrics"><div><strong>${recordingDuration(mode==='review'?r.review?.duration_s:r.elapsed_s)}</strong><span>片段时长</span></div><div><strong>${finite(mode==='review'?r.review?.frames:r.frames)?(mode==='review'?r.review.frames:r.frames).toLocaleString():'—'}</strong><span>已采集帧</span></div></div><div class="recorder-actions">${!app.authenticated?'<button class="button" data-click="login">输入控制码启用录制</button>':''}${['not_started','idle','unavailable'].includes(mode)?`<button class="button primary" data-recording="start" ${!canStart||!app.authenticated?'disabled':''}>${mode==='not_started'?'启动独立录制服务':'开始录制'} ${icon('plus')}</button>`:''}${mode==='recording'?`<button class="button recorder-stop" data-recording="stop" ${!canStop||!app.authenticated?'disabled':''}>结束并审核</button>`:''}${mode==='review'?`<button class="button primary" data-recording="save" ${!canSave||!app.authenticated?'disabled':''}>保存这条录制 ${icon('download')}</button>`:''}${['starting','finalizing'].includes(mode)?`<div class="recorder-processing"><span class="recording-spinner"></span>${mode==='starting'?'正在准备采集…':'正在整理片段，请稍候…'}</div>`:''}${mode==='error'?'<p class="review-note">本条暂不接受新操作。请先查看下方恢复信息。</p>':''}</div></div></div>${!app.recordingOnline?'<div class="notice warn mt">未能获取录制器最新状态。不要重复启动；连接恢复后将重新显示真实状态。</div>':''}${reasons.length&&mode!=='recording'?`<div class="recording-reasons"><strong>${mode==='review'?'片段检查':'开始前检查'}</strong><ul>${reasons.map(reason=>`<li>${esc(typeof reason==='string'?reason:reason.message||reason.reason||JSON.stringify(reason))}</li>`).join('')}</ul></div>`:''}${r.last_error?`<div class="notice warn mt">${esc(r.last_error)}</div>`:''}${r.last_saved&&mode!=='review'?`<div class="recording-last-saved"><strong>${r.last_saved.kind==='recovery'?'上次保存：恢复数据':'上次保存：正式片段'}</strong><span>${esc(r.last_saved.relative_path||r.last_saved.path||r.last_saved.id||'已保存在独立目录')}</span>${r.last_saved.kind==='recovery'?'<p>本条未通过完整性检查，已保留原始观测用于排查，不进入正式训练数据。</p>':''}</div>`:''}${r.review&&mode==='review'?`<div class="recording-review-summary"><strong>${r.review.raw_save_allowed?'原始采集已结束 · 待保存':r.review.coverage_fault?'采集覆盖检查发现问题':r.review.training_ready?'采集检查通过':'完整性未通过，将保存为恢复数据'}</strong><p>${r.review.raw_save_allowed?'保存后可在文件夹中检查原始三图；转换和训练审核单独进行。':r.review.coverage_fault?esc(typeof r.review.coverage_fault==='string'?r.review.coverage_fault:JSON.stringify(r.review.coverage_fault)):(r.review.training_ready?'保存后可在「数据」页查看已生成的视频，并补充审核结论。':'保存会保留当前观测供排查，不进入正式 LeRobot 训练片段。')}</p><span>训练可用性仍以数据合同验收为准。</span></div>${r.review.raw_save_allowed?'':episodeVideos(r.review.videos,{...r.review,pending:true,duration_s:r.review.duration_s})}`:''}`;
 mountMediaViewers(holder);
}
async function refreshRecording(){
 if(app.recordingPollBusy)return;app.recordingPollBusy=true;
 try{const result=await api('/api/recording/state');app.recording=result.recording;app.recordingOnline=true;}
 catch{app.recordingOnline=false;}
 finally{app.recordingPollBusy=false;renderRecording();}
}
async function recordingAction(action){
 if(!['start','stop','save'].includes(action)||app.recordingBusy)return;
 if(!app.authenticated){await login();return;}
 app.recordingBusy=true;renderRecording();
 try{const result=await api(`/api/recording/${action}`,{},{timeout:120000});if(result.recording){app.recording=result.recording;app.recordingOnline=true;}await refreshRecording();
  if(action==='start')toast(app.recording?.mode==='idle'?'独立录制服务已启动；条件就绪后点击开始录制':app.recording?.mode==='recording'?'独立上肢录制已开始':'启动请求已提交，请查看录制器状态');
  if(action==='stop')toast(app.recording?.mode==='review'?'本条已结束，请检查片段后明确保存':'结束请求已处理，请查看录制器状态');
  if(action==='save'){toast(app.recording?.last_saved?.kind==='recovery'?'本条已保存为恢复数据，未进入正式训练片段':'录制已保存至独立 LeRobot 目录');await refreshData();const saved=app.recording?.last_saved;if(saved?.id&&saved.kind!=='recovery'&&app.page==='data')await selectEpisode(saved.id);}
 }catch(error){toast(error.message,true);await refreshRecording();}
 finally{app.recordingBusy=false;renderRecording();}
}
function formatBytes(bytes){if(!finite(bytes))return '—';return bytes>=1e9?`${fmt(bytes/1e9,2)} GB`:bytes>=1e6?`${fmt(bytes/1e6,1)} MB`:`${fmt(bytes/1e3,1)} KB`;}
function formatDate(value){if(!value)return '未记录';const date=new Date(typeof value==='number'&&value<1e12?value*1000:value);return Number.isNaN(date.getTime())?'未记录':date.toLocaleString('zh-CN',{hour12:false});}
function reviewLabel(value){return {pending:'待审核',approved:'已通过',rejected:'需排查'}[value]||'待审核';}
function renderDataStorage(){
 const holder=$('#data-storage');if(!holder)return;
 const stale=!app.apiOnline||Date.now()-app.lastReceived>10000||!!app.dataReadFailed;
 const html=storageHTML(app.data,app.state?.system_health?.disk,{stale});
 if(holder.innerHTML!==html)holder.innerHTML=html;
}
function updateData(){
 if(app.page!=='data')return;renderDataStorage();const d=app.state?.data||{},recording=app.data?.recording||{};
 const status={observed_active:'检测到录制相关进程',no_process_observed:'未检测到录制进程',unknown:'状态未确认'}[recording.state]||d.recorder_status||'正在读取';
 $('#data-status').innerHTML=`<div class="line-list">${lineList([['录制状态',status],['本地目录',app.data?.root||d.local_path||d.path||'未读取'],['自动上传',d.transfer_status||'未配置'],['中央平台',d.central_status||'未配置']])}</div>`;
}
async function refreshData(){
 if(app.dataBusy)return;app.dataBusy=true;
 try{app.data=await api('/api/data');app.dataReadFailed=false;if(app.page==='data'){const selected=app.data.episodes?.find(e=>e.id===app.selectedEpisode);if(selected&&app.episodeDetail&&!app.reviewDirty&&selected.source_signature===app.episodeDetail.source_signature){app.episodeDetail={...app.episodeDetail,videos:selected.videos||app.episodeDetail.videos,analytics_url:selected.analytics_url||app.episodeDetail.analytics_url,preview_status:selected.preview_status,duration_s:selected.duration_s};}renderDataCatalog();updateData();}}
 catch(error){app.dataReadFailed=true;renderDataStorage();if(app.page==='data'){$('#data-folders').innerHTML=empty('暂时无法读取数据',error.message,'data');$('#episode-count').textContent='读取失败';}}
 finally{app.dataBusy=false;}
}
let dataSelecting=false,dataDeleting=false;
const dataFilters={query:'',review:'all',kind:'recordings',page:1};
const dataSelection=new Map();
const deletable=e=>['lxl','hil_demonstration','hil_policy_hil','pico_capture'].includes(e.source);
function resetDataSelection(){dataSelecting=false;dataSelection.clear();}
function toggleDataSelection(id){
 if(dataDeleting)return;
 const e=app.data?.episodes?.find(e=>e.id===id);if(!e||!deletable(e))return;
 if(dataSelection.has(id))dataSelection.delete(id);else {if(dataSelection.size>=100){toast('每次最多删除 100 条');return;}dataSelection.set(id,{id,source_signature:e.source_signature,name:e.name});}
 renderDataCatalog();
}
async function deleteSelectedData(){
 if(dataDeleting||!dataSelection.size)return;
 const selected=[...dataSelection.values()];
 if(!await login())return;
 if(!confirm(`永久删除 F1 上的 ${selected.length} 条录制？\n\n${selected.map(e=>e.name).join('\n')}\n\n将删除所选原始数据、对应本机回放、审核及派生任务。无法撤销。独立训练导出、恢复副本和服务器副本保留。`))return;
 dataDeleting=true;renderDataCatalog();
 try{
  const result=await api('/api/data/delete',{confirm:true,episodes:selected.map(({id,source_signature})=>({id,source_signature}))},{timeout:60000});
  const deleted=result.results.filter(r=>r.state==='deleted');
  if(result.results.some(r=>r.id===app.selectedEpisode&&r.state!=='pending')){
   disposeMediaViewers($('#episode-detail'));app.selectedEpisode=null;app.episodeDetail=null;
   if($('#episode-detail')){$('#episode-detail').innerHTML='';delete $('#episode-detail').dataset.episodeSignature;}
  }
  resetDataSelection();
  if(result.complete)toast(`已从 F1 删除 ${deleted.length} 条录制`);
  else alert(`已完成 ${deleted.length} 条；其余清理未完成，请保留记录号 ${result.transaction}：\n`+result.results.filter(r=>r.state!=='deleted').map(r=>`${r.name}：${r.error||r.state}`).join('\n'));
 }finally{dataDeleting=false;await Promise.allSettled([refreshData(),refreshState(),refreshSystem()]);renderDataCatalog();}
}
function dataLibraryPage(){return libraryPage(libraryView(app.data,app.dataFolder||''),dataFilters);}
function showDataWorkspace(){
 const detail=!!app.selectedEpisode;$('#data-library-view').hidden=detail;$('#episode-detail').hidden=!detail;
}
function returnDataLibrary(){
 const holder=$('#episode-detail');disposeMediaViewers(holder);app.selectedEpisode=null;app.episodeDetail=null;app.reviewDirty=false;holder.innerHTML='';delete holder.dataset.episodeSignature;
 history.replaceState(null,'','#data');renderDataCatalog();$('#data-search')?.focus({preventScroll:true});
}
function renderDataCatalog(){
 if(app.page!=='data')return;const data=app.data||{};renderDataStorage();showDataWorkspace();
 const view=libraryView(data,app.dataFolder||'');app.dataFolder=view.current||null;
 const page=libraryPage(view,dataFilters);dataFilters.page=page.page;
 $('#episode-count').textContent=dataFilters.kind==='exports'?`${view.exports.length} 份导出`:`${view.records.length} 条录制`;
 $$('[data-data-kind]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.dataKind===dataFilters.kind)));
 for(const id of dataSelection.keys())if(!view.leaves.some(e=>e.id===id))dataSelection.delete(id);
 const selectable=page.rows.filter(e=>deletable(e)&&view.leaves.some(leaf=>leaf.id===e.id));
 const selectActions=(selectable.length||dataSelecting)?`<div class="data-select-actions">${dataSelecting?`<span class="data-selection-count">已选 ${dataSelection.size} 条</span><button class="button small" data-click="data-select-all" ${dataDeleting||!selectable.length?'disabled':''}>${selectable.length&&selectable.every(e=>dataSelection.has(e.id))?'取消本页':'全选本页'}</button><button class="button small danger" data-click="data-delete" ${!dataSelection.size||dataDeleting?'disabled':''}>${dataDeleting?'删除中…':'删除所选'}</button><button class="button small" data-click="data-select-cancel" ${dataDeleting?'disabled':''}>取消</button>`:`<button class="button small" data-click="data-select-start">选择</button>`}</div>`:'';
 const crumbs=[`<button data-folder="">全部录制</button>`];
 for(let i=0;i<view.prefix.length;i++)crumbs.push(`<span>/</span><button data-folder="${esc(view.prefix.slice(0,i+1).join('/'))}">${esc(view.prefix[i])}</button>`);
 const folders=dataFilters.kind==='exports'?'':view.folders.map(f=>`<button class="data-source-button" data-folder="${esc(f.path)}">${icon('data')}<span>${esc(f.name)}</span><small>${f.count}</small></button>`).join('');
 const rows=page.rows.map(e=>{
  const selecting=dataSelecting&&selectable.some(row=>row.id===e.id),checked=dataSelection.has(e.id),source={hil_demonstration:'人工示教',hil_policy_hil:'策略 HIL',pico_capture:'Pico',lxl:'LXL',lerobot:'独立上肢',unified_dataset:'LeRobot v3'}[e.source]||'其他';
  return `<tr class="${checked?'delete-selected':''}"><td class="recording-name-cell">${selecting?`<button class="recording-checkbox" data-select-episode="${esc(e.id)}" role="checkbox" aria-checked="${checked}" aria-label="选择 ${esc(e.name)}" ${dataDeleting?'disabled':''}>${checked?'✓':''}</button>`:''}<button class="recording-open" data-episode="${esc(e.id)}" ${dataDeleting?'disabled':''} title="${esc(e.name)}">${icon('data')}<span>${esc(e.name)}</span></button></td><td class="recording-source">${esc(source)}</td><td class="recording-date">${esc(formatDate(e.started_at||e.modified_at))}</td><td class="recording-duration">${finite(e.duration_s)?`${fmt(e.duration_s,1)} s`:'—'}</td><td class="recording-size">${formatBytes(e.size_bytes)}</td><td>${pill(reviewLabel(e.review?.status),e.review?.status==='approved'?'good':e.review?.status==='rejected'?'warn':'neutral')}</td></tr>`;
 }).join('');
 const html=`<div class="data-browser-toolbar"><nav class="data-breadcrumb" aria-label="数据来源">${dataFilters.kind==='exports'?'<strong>训练导出</strong>':crumbs.join('')}</nav>${selectActions}</div>${folders?`<div class="data-source-folders">${folders}</div>`:''}<div class="recording-table-wrap"><table class="recording-table"><thead><tr><th>录制名称</th><th class="recording-source">来源</th><th class="recording-date">录制时间</th><th class="recording-duration">时长</th><th class="recording-size">大小</th><th>审核</th></tr></thead><tbody>${rows||`<tr><td colspan="6">${empty(dataFilters.query||dataFilters.review!=='all'?'没有匹配的录制':'暂无录制','','data')}</td></tr>`}</tbody></table></div><div class="data-pagination"><span>${page.total?`${page.start}–${page.end} / ${page.total}`:'0'} ${dataFilters.kind==='exports'?'份':'条'}</span><div><button class="button small" data-data-page="${page.page-1}" ${page.page<=1||dataDeleting?'disabled':''} aria-label="上一页">上一页</button><span>${page.page} / ${page.pages}</span><button class="button small" data-data-page="${page.page+1}" ${page.page>=page.pages||dataDeleting?'disabled':''} aria-label="下一页">下一页</button></div></div>`;
 const holder=$('#data-folders');if(holder.innerHTML!==html)holder.innerHTML=html;
 if(app.episodeDetail&&app.selectedEpisode===app.episodeDetail.id&&!app.reviewDirty)renderEpisodeDetail();
}
async function selectEpisode(id){
 const holder=$('#episode-detail');disposeMediaViewers(holder);app.selectedEpisode=id;app.episodeDetail=null;app.reviewDirty=false;resetDataSelection();showDataWorkspace();delete holder.dataset.episodeSignature;
 holder.innerHTML=`<button class="button small data-back" data-click="data-back">← 返回录制库</button><section class="card">${empty('正在打开录制','','data')}</section>`;
 holder.scrollIntoView({block:'start'});
 try{const result=await api(`/api/data/episodes/${encodeURIComponent(id)}`);if(app.selectedEpisode!==id||app.page!=='data')return;app.episodeDetail=result.episode;renderEpisodeDetail();}catch(error){if(app.page==='data'&&app.selectedEpisode===id)holder.innerHTML=`<button class="button small data-back" data-click="data-back">← 返回录制库</button><section class="card">${empty('无法打开录制',error.message,'data')}</section>`;}
}
function renderEpisodeDetail(){
 const e=app.episodeDetail;if(app.page!=='data'||!e)return;const holder=$('#episode-detail'),signature=JSON.stringify(e);showDataWorkspace();if(holder.dataset.episodeSignature===signature)return;holder.dataset.episodeSignature=signature;disposeMediaViewers(holder);
 const preview=typeof e.preview_url==='string'&&e.preview_url.startsWith('/previews/')?e.preview_url:null,review=e.review||{},topics=e.topics||[],files=e.files||[];
 const media=e.replay_url?replayHTML(e,esc)+(e.videos?.length?`<details class="original-frame-review data-video-copy"><summary>连续视频与曲线 <span>压缩副本</span></summary>${episodeVideos(e.videos,e)}</details>`:''):e.source?.startsWith('hil_')?(e.recording_status==='committed'?'<p class="notice">录制已保存，图像回看暂不可用。</p>':'<p class="notice warn">录制未完整提交，原件保留待排查。</p>'):episodeVideos(e.videos,e);
 holder.innerHTML=`<button class="button small data-back" data-click="data-back">← 返回录制库</button><section class="card episode-workspace"><div class="episode-heading"><div><h2 title="${esc(e.name)}">${esc(e.name)}</h2><div class="episode-meta"><span data-episode-review>${pill(reviewLabel(review.status),review.status==='approved'?'good':review.status==='rejected'?'warn':'neutral')}</span><span>${esc(formatDate(e.started_at||e.modified_at))}</span><span>${formatBytes(e.size_bytes)}</span>${finite(e.duration_s)?`<span>${fmt(e.duration_s,1)} 秒</span>`:''}${e.source?.startsWith('hil_')?`<span>${e.execution==='powered'?'实机记录':'演练记录'}</span>`:''}</div></div><div class="episode-actions">${e.source==='hil_demonstration'?'<button class="button primary" data-motion-replay-open aria-expanded="false">实机 Replay</button>':''}${preview?`<a class="button" href="${esc(preview)}" target="_blank" rel="noopener">抽帧预览 ${icon('arrow')}</a>`:''}</div></div>${motionReplayHTML(e)}<div class="episode-media">${media}</div><div class="episode-support"><form class="review-form" id="review-form"><div class="review-form-heading"><h3>录制审核</h3><span id="review-saved" class="review-note">${review.updated_at?`已保存 ${esc(formatDate(review.updated_at))}`:''}</span></div><div class="review-fields"><label>审核结论<select id="review-status">${[['pending','待审核'],['approved','通过'],['rejected','需排查']].map(([value,label])=>`<option value="${value}" ${review.status===value?'selected':''}>${label}</option>`).join('')}</select></label><label class="review-notes-field">备注<textarea id="review-notes" maxlength="4000" rows="2" placeholder="添加审核备注">${esc(review.notes||'')}</textarea></label><button type="submit" class="button primary">保存审核</button></div>${review.stale?'<p class="notice warn">来源文件已变化，请重新审核。</p>':''}</form><div class="episode-metadata"><details class="episode-files"><summary>文件明细 <span>${files.length} 个文件 · ${formatBytes(e.size_bytes)}</span></summary><div class="episode-files-scroll"><table class="data-table"><thead><tr><th>文件名</th><th>大小</th><th>更新时间</th></tr></thead><tbody>${files.map(file=>`<tr><td>${esc(file.relative_path||file.name)}</td><td>${formatBytes(file.size_bytes)}</td><td>${esc(formatDate(file.modified_at))}</td></tr>`).join('')||'<tr><td colspan="3">没有可显示的文件</td></tr>'}</tbody></table></div></details><details class="episode-path"><summary>存储路径</summary><code>${esc(e.path||e.relative_path||'未报告')}</code></details>${topics.length||finite(e.message_count)?`<details class="topic-list"><summary>消息主题 <span>${topics.length} 个${finite(e.message_count)?` · ${e.message_count.toLocaleString()} 条消息`:''}</span></summary><div class="episode-topics-scroll">${topics.map(topic=>`<div class="topic-entry"><span>${esc(topic.name)}</span><small>${esc(topic.type)} · ${esc(topic.message_count??'—')} 条</small></div>`).join('')}</div></details>`:''}</div></div></section>`;
 $('#review-form').addEventListener('submit',event=>{event.preventDefault();run(saveReview);});mountMediaViewers(holder);
 const motionRoot=holder.querySelector('[data-motion-replay]');if(motionRoot){motionReplay=new MotionReplay(motionRoot,e,{api,run,acquire,release,stop,expanded:open=>{const entry=holder.querySelector('[data-motion-replay-open]');entry.classList.toggle('primary',!open);entry.setAttribute('aria-expanded',String(open));},state:()=>app.state,lease:()=>ownLease()?app.lease:null,executing:()=>app.executing,refresh:refreshState,accept:action=>{app.activeAction=action;app.submittedDraft=null;app.actionContext={id:action.id,title:'实机 Replay',estimatedTotal:null};}});holder.querySelector('[data-motion-replay-open]').onclick=()=>{motionReplay.open();motionRoot.scrollIntoView({behavior:'smooth',block:'nearest'});};}
 const replay=holder.querySelector('[data-hil-replay]');if(replay)activeMediaPlayers.add(new HILReplay(replay));
}
async function saveReview(){
 if(!app.authenticated){await login();return;}
 const id=app.selectedEpisode;if(!id)return;const status=$('#review-status').value,notes=$('#review-notes').value,button=$('button[type=submit]',$('#review-form'));button.disabled=true;
 try{const result=await api('/api/data/reviews',{episode_id:id,status,notes,expected_source_signature:app.episodeDetail?.source_signature});if(app.selectedEpisode===id){app.reviewDirty=false;app.episodeDetail.review=result.review;$('#episode-detail').dataset.episodeSignature=JSON.stringify(app.episodeDetail);$('#review-saved').textContent=`已保存 ${formatDate(result.review.updated_at)}`;const marker=$('[data-episode-review]');if(marker)marker.innerHTML=pill(reviewLabel(result.review.status),result.review.status==='approved'?'good':result.review.status==='rejected'?'warn':'neutral');}toast('审核结果已保存，原始录制保持不变');await refreshData();}finally{if(button.isConnected)button.disabled=false;}
}
function renderEvents(holder){if(!holder)return;holder.innerHTML=app.events.length?[...app.events].slice(0,40).map(event=>{const stamp=event.at||event.time||event.timestamp||event.created_at;const date=stamp?new Date(typeof stamp==='number'&&stamp<1e12?stamp*1000:stamp):null;const time=date&&!Number.isNaN(date.getTime())?date.toLocaleTimeString('zh-CN',{hour12:false}):'—';return `<div class="event"><time>${esc(time)}</time><p><span class="event-type">${esc(event.type||event.kind||'事件')}</span>${esc(event.message||event.detail||event.error||event.action_id||'已记录')}</p></div>`;}).join(''):empty('暂无操作记录','动作结果和控制权变化会记录在这里。');}
function updateSystem(){
 if(app.page!=='system')return;const r=app.robot||{},s=app.system||{};
 const items=[['机器人',r.id||'未读取'],['机器标签',r.label||'未配置'],['局域网 IP',r.ip||'未读取'],['服务版本',s.version||r.version||'未读取'],['运行模式',s.mode||'以本机配置为准'],['消息 / 环境',s.ros_distro||s.ros?.distro||'未报告']];
 $('#system-identity').innerHTML=items.map(([key,value])=>`<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join('');
 if(r.ip&&/^[\d.]+$/.test(r.ip))$('#vendor-link').href=`http://${r.ip}/`;
 $('#session-status').innerHTML=lineList([['访问',app.authenticated?'已启用操作':'只读观察'],['控制权',app.lease?'由当前浏览器持有':app.state?.control?.owner||'未持有'],['可用情况',app.state?.control?.reason||'以实际请求检查为准']]);
 const services=app.state?.services||s.services||{};$('#system-services').innerHTML=Object.keys(services).length?Object.entries(services).map(([name,entry])=>{const status=statusFor(entry,'service');return `<div class="line-item"><span>${esc(name)}</span>${pill(status.text,status.type)}</div>`;}).join(''):empty('尚无服务清单','等待本机服务回报运行状态。');
 $('#revision-label').textContent=app.revision?String(app.revision).slice(0,18):'等待检测';renderEvents($('#system-events'));
}
function updatePanels(){motionReplay?.update();updateOverview();updateControl();updatePico();updateData();renderRecording();updateSystem();}
function updateGlobal(){
 const s=app.state,connected=app.apiOnline;$('#connection-pill').className=`pill ${connected?'good':'neutral'}`;$('#connection-pill').innerHTML=`<span class="small-dot"></span>${connected?'服务在线':'未连接'}`;$('#sidebar-status').textContent=connected?'本地网关已连接':'等待本地服务';$('#sidebar-dot').style.color=connected?'#a9d79a':'#849087';$('#robot-label').textContent=app.robot?.id||'F1';$('#updated-at').textContent=app.lastReceived?`最近同步 ${new Date(app.lastReceived).toLocaleTimeString('zh-CN',{hour12:false})}`:'尚未同步';
 $('#stop-button').disabled=!ownLease();
 const boundary=headBoundaryPhase()&&actionRunning();
 $('#stop-button').textContent=app.page==='hil'?'停止工作台动作':boundary?'取消后续动作':'停止当前动作';
 $('#stop-button').title=boundary?'取消后续动作；不能撤回驱动已接收的头部边界目标':'只停止本工作台拥有的动作，不代替硬件急停';
 $$('[data-click="stop"]').forEach(el=>{el.textContent=boundary?'取消后续动作':'停止动作';el.title=boundary?'不能撤回已接收的头部边界目标':'';});
 document.body.classList.toggle('control-mode',ownLease());if($('#control-mode-exit'))$('#control-mode-exit').hidden=!ownLease();
 if($('#pose-control-status'))$('#pose-control-status').hidden=!ownLease();
 const busy=poseBusy();$$('[data-target-input],[data-target-slider],[data-waist-delta],[data-waist-step],[data-preset-index],[data-preset-delete],[data-click="align"],[data-click="new-preset"]').forEach(el=>{const name=el.dataset.targetSlider,range=name?.startsWith('gripper_')?[0,100]:app.robot?.joint_limits_deg?.[name];el.disabled=busy||Boolean(name&&(!Array.isArray(range)||app.draft[name]===undefined||app.draft[name]===''));});
 $$('[data-execute-required]').forEach(el=>{el.disabled=busy||actionRunning(s?.action)||!connected||!s?.connected||(!ownLease()&&Boolean(s?.control?.owner))||(app.group!=='base'&&!app.dirty.size&&app.waistIntent===null);el.innerHTML=actionRunning()?'执行中…':app.preflighting?'检查中…':`执行 ${icon('arrow')}`;});
 $$('[data-click="stop"]').forEach(el=>el.disabled=!ownLease());
 const awaitingDriver=connected&&s?.services?.motor_ctl?.running===false&&!Object.keys(s?.joints||{}).length;const warning=!connected?'工作台服务暂不可达，当前显示的是最后收到的状态。':s?.error?String(s.error):awaitingDriver?'网页已启动，但尚未检测到本体关节驱动。当前关节状态与姿态控制不可用，请检查机器人的基础启动流程。':'';$('#global-alert').hidden=!warning;$('#global-alert').textContent=warning;
 if($('#lease-heading')){
   $('#lease-heading').textContent=ownLease()?'当前浏览器持有控制权':s?.control?.owner?`当前控制者：${s.control.owner}`:'观察模式';
   $('#lease-description').textContent=ownLease()?'操作心跳已建立。释放后可切换至现场 Pico；失联后由本机租约期限处理。':s?.control?.reason||'输入控制码取得控制权。滑块编辑后仍需预览并确认执行。';
   $('#lease-panel').classList.toggle('has-control',ownLease());$('#acquire-button').hidden=Boolean(app.lease);$('#acquire-button').disabled=!connected;$('#release-button').hidden=!app.lease;
 }
 const capKind=app.group==='base'?'base':app.group==='grippers'?'gripper':'joints';let capability=s?.capabilities?.[capKind]||app.robot?.capabilities?.[capKind];if(capKind==='joints'&&[...app.dirty].filter(n=>!n.startsWith('gripper_')).some(n=>!app.robot?.joint_limits_deg?.[n]))capability={available:false,reason:'本机此部位尚未完成动作核验；当前可读反馈、编辑并保存预设。'};$$('[data-motion-required],[data-jog]').forEach(el=>el.disabled=poseBusy()||!canMove()||capability?.available===false);if($('#capability-note')){$('#capability-note').hidden=capability?.available!==false;$('#capability-note').textContent=capability?.reason||'当前机器配置尚未启用此部位执行，仍可读取反馈和编辑目标。';}if($('[data-execute-required]')&&(capability?.available===false||(app.group==='lift'&&(app.waistMode==='cartesian'?!app.waistPreview:app.liftPercent===null))))$('[data-execute-required]').disabled=true;$$('[data-auth-required]').forEach(el=>el.disabled=!app.authenticated||!connected);
}
function reconcileStateAction(previous,state,requestedAction){
 if(!Object.prototype.hasOwnProperty.call(state,'action')||app.executing||app.pendingAction||app.preflighting||app.activeAction?.id!==requestedAction)return;
 const current=app.activeAction;if(!current||state.action?.id===current.id)return;
 // A completed record is history. A disappeared running record is not proof of
 // stopping; retain the draft and require a new explicit execution.
 const lostRunning=actionRunning(current)&&(previous?.action?.id===current.id||(previous?.authenticated===true&&state.authenticated===false));
 if(!actionRunning(current)||lostRunning){
  app.activeAction=null;app.actionContext=null;app.submittedDraft=null;
  app.poseNotice=state.action?'机器人状态已更新，请以当前实测为准':'工作台状态已重置，请检查实测后重新执行';
 }
}
async function refreshState(){
 if(app.stateBusy)return;app.stateBusy=true;const requestedLease=app.lease,requestedAction=app.activeAction?.id;
 try{
  const state=await api('/api/state'),previous=app.state;app.state=state;seedLiveTargets();app.apiOnline=true;app.lastReceived=Date.now();
  if(typeof state.authenticated==='boolean')app.authenticated=state.authenticated;
  if(app.lease&&app.lease===requestedLease&&state.control?.lease_id!==app.lease)app.lease=null;
  reconcileStateAction(previous,state,requestedAction);maybeRefreshPoseReview();updatePanels();
 }catch(error){app.apiOnline=false;await stopJog();app.lease=null;}
 finally{app.stateBusy=false;updateGlobal();}
}
async function refreshPresets(){const result=await api('/api/presets');app.presets=Array.isArray(result.presets)?result.presets:[];renderPresets();}
async function refreshEvents(){try{const result=await api('/api/events');app.events=Array.isArray(result.events)?result.events:[];for(const id of ['event-list','system-events'])renderEvents($(`#${id}`));}catch{}}
async function refreshSystem(){try{const [result,identity]=await Promise.all([api('/api/system'),api('/api/robot')]);app.system=result.system||result;app.robot=identity.robot;updateSystem();updatePico();updateGlobal();}catch{}}
async function heartbeat(){if(!app.lease)return;try{await api('/api/control/heartbeat',{lease_id:app.lease});}catch(error){app.lease=null;stopJog();updateGlobal();toast(`控制权已释放：${error.message}`,true);}}
async function login(){
 if(app.authenticated)return true;
 let dialog=$('#login-dialog');if(!dialog){dialog=document.createElement('dialog');dialog.id='login-dialog';dialog.className='execute-dialog';dialog.innerHTML='<form id="login-form"><div class="dialog-kicker">操作会话</div><h2>启用操作</h2><p>输入控制码启用遥操、录制和保存操作。登录本身不会启动动作。</p><label class="field-label mt" for="access-token">控制码</label><input id="access-token" type="password" autocomplete="off" required style="width:100%"><p id="login-error" role="alert"></p><div class="dialog-actions"><button type="button" class="button" id="login-cancel">取消</button><button type="submit" class="button primary">启用操作</button></div></form>';document.body.append(dialog);$('#login-cancel').onclick=()=>dialog.close();dialog.addEventListener('close',()=>{$('#access-token').value='';});$('#login-form').onsubmit=async event=>{event.preventDefault();const submit=$('button[type=submit]',dialog);submit.disabled=true;try{await api('/api/login',{control_code:$('#access-token').value});$('#access-token').value='';app.authenticated=true;dialog.close();toast('操作会话已启用，请选择操作并按页面确认');updateGlobal();updateSystem();}catch(error){$('#login-error').textContent=error.message;}finally{submit.disabled=false;}};}
 $('#login-error').textContent='';$('#access-token').value='';dialog.showModal();$('#access-token').focus();return new Promise(resolve=>dialog.addEventListener('close',()=>resolve(app.authenticated),{once:true}));
}
async function acquire(){
 let dialog=$('#control-code-dialog');if(!dialog){dialog=document.createElement('dialog');dialog.id='control-code-dialog';dialog.className='execute-dialog';dialog.innerHTML='<form id="control-code-form"><div class="dialog-kicker">现场控制</div><h2>取得控制权</h2><p>输入控制码接管当前浏览器。取得控制权不会启动动作。</p><label class="field-label mt" for="control-code">控制码</label><input id="control-code" type="password" autocomplete="off" required><p id="control-code-error" role="alert"></p><div class="dialog-actions"><button type="button" class="button" id="control-code-cancel">取消</button><button type="submit" class="button primary">确认接管</button></div></form>';document.body.append(dialog);$('#control-code-cancel').onclick=()=>dialog.close();dialog.addEventListener('close',()=>{$('#control-code').value='';});$('#control-code-form').onsubmit=async event=>{event.preventDefault();const button=$('button[type=submit]',dialog);button.disabled=true;try{const result=await api('/api/control/acquire',{control_code:$('#control-code').value,owner:'browser'});app.authenticated=Boolean(result.authenticated??true);app.lease=result.lease_id;dialog.close();toast('已取得控制权，请预览并确认需要执行的目标');await refreshState();}catch(error){$('#control-code-error').textContent=error.message;$('#control-code').value='';}finally{button.disabled=false;}};}
 $('#control-code-error').textContent='';$('#control-code').value='';dialog.showModal();$('#control-code').focus();return new Promise(resolve=>dialog.addEventListener('close',()=>resolve(ownLease()),{once:true}));
}
async function release(){await stopJog();if(app.lease){const lease=app.lease;await api('/api/control/release',{lease_id:lease});app.lease=null;toast('已释放控制权');}updateGlobal();await refreshState();}
function readTargets(names){const targets={};for(const name of names){const value=app.draft[name];if(value!==''&&value!==undefined&&value!==null){const number=Number(value);if(!Number.isFinite(number))throw new Error(`${labelJoint(name)}目标不是有效数字`);const range=app.robot?.limits?.[name];if(Array.isArray(range)&&(number<range[0]||number>range[1])&&!app.dirty.has(name))continue;if(Array.isArray(range)&&(number<range[0]||number>range[1]))throw new Error(`${labelJoint(name)}目标超出本机配置范围 ${range[0]}～${range[1]}°`);targets[name]=number;}}if(!Object.keys(targets).length)throw new Error('请先输入目标，或从新鲜实测填入');return targets;}
async function previewAction(action){
 if(!app.apiOnline||!app.state?.connected)throw new Error('请等待机器人反馈接入');
 if(app.executing||app.activeAction&&['accepted','queued','running','stopping'].includes(app.activeAction.status))throw new Error('请等待当前动作结束');
 const recovery=action.kind==='pose'?headBoundaryPlan(app.posePlan):null;
 if(ownLease()&&!recovery)return runAuthorizedAction(action);
 app.pendingAction=structuredClone(action);app.pendingHeadRecovery=recovery;
 const dialog=$('#execute-dialog'),needsCode=configureExecutionAuthorization();
 $('h2',dialog).textContent=recovery?'确认头部回程并执行':'进入操控模式并执行';
 $('.muted',dialog).textContent=recovery?'仅在机器人旁确认；确认后执行本次已显示的完整目标。':'确认现场空间后授权执行。操控模式保持有效，退出后需重新授权。';
 $('#confirm-execute').textContent=recovery?'确认回程并执行':'确认执行';
 $('#execute-description').style.whiteSpace='pre-line';
 $('#execute-description').textContent=action.kind==='base_authorize'?'授权后请重新按住方向键点动；确认授权本身不会移动底盘。':action.kind==='gripper'?`${action.side==='left'?'左':'右'}夹爪 · 固定速度 25%/s`:`${Object.keys(action.targets||{}).length} 个关节目标 · 固定速度执行。`;
 if(action.kind==='pose')$('#execute-description').textContent=recovery?
  `头部 J2 当前 ${fmt(recovery.origin,2)}°，先只向 head_j2 下发合法边界目标 ${fmt(recovery.edge,0)}°。\n这一步没有软件速度保证，不按固定速度轨迹执行。确认进入工作范围后，才继续其余姿态目标。\n取消不能撤回驱动已接收的边界目标，只能阻止后续动作；不代表头部已停稳。`:
  `${pendingTitle()} · ${(app.posePlan?.parts||[]).map(p=>p.label).join('、')}${finite(estimatedTime(app.posePlan))?` · 预计 ${fmt(estimatedTime(app.posePlan),1)} 秒`:''}`;
 const targets=action.kind==='gripper'?{[`${action.side==='left'?'左':'右'}夹爪`]:action.position}:action.targets||{};
 $('#execute-targets').innerHTML=Object.entries(targets).map(([name,value])=>`<div><small>${esc(action.kind==='gripper'?name:labelJoint(name))}</small>${esc(value)}${action.kind==='gripper'?'%':'°'}</div>`).join('');
 $('#execute-code').value='';$('#execute-error').textContent='';$('#confirm-execute').disabled=false;dialog.showModal();(needsCode?$('#execute-code'):$('#cancel-execute')).focus();updateGlobal();
}
async function submitAuthorizedAction(action){
 if(action.kind==='base_authorize'){toast('操控模式已启用，请按住方向键移动，松手即停');return;}
 if(['joints','lift','waist_height','pose'].includes(action.kind)){
  const response=await api('/api/actions/plan',{...action,lease_id:app.lease});
  if(headBoundaryPlan(response.plan)&&action.head_boundary_recovery_ack!==true)throw new Error('当前状态需要头部回程，请重新点击执行并确认');
 }
 try{await executeAction(action);}catch(e){app.poseError=e.message;updatePoseSummary();throw e;}
}
async function runAuthorizedAction(action){
 if(app.executing)return;app.executing=true;updateGlobal();
 try{await submitAuthorizedAction(action);}catch(e){app.poseError=e.message;updatePoseSummary();throw e;}
 finally{app.executing=false;updateGlobal();}
}
async function confirmExecution(event){
 event.preventDefault();if(app.executing||!app.pendingAction)return;
 if(!ownLease()&&$('#execute-code').disabled){configureExecutionAuthorization();$('#execute-error').textContent='控制权已失效，请输入控制码后再次确认';$('#execute-code').focus();return;}
 app.executing=true;$('#confirm-execute').disabled=true;$('#cancel-execute').disabled=true;
 const action=structuredClone(app.pendingAction);
 if(app.pendingHeadRecovery)action.head_boundary_recovery_ack=true;
 try{
  if(!ownLease()){
   const acquired=await api('/api/control/acquire',{control_code:$('#execute-code').value,owner:'browser'});
   $('#execute-code').value='';app.authenticated=true;app.lease=acquired.lease_id;updateGlobal();
  }
  await submitAuthorizedAction(action);
  $('#execute-dialog').close();
 }catch(error){
  app.poseError=error.message;updatePoseSummary();
  $('#execute-code').value='';
  if(ownLease())$('#execute-dialog').close();
  else $('#execute-error').textContent=error.message;
 }finally{app.executing=false;$('#confirm-execute').disabled=false;$('#cancel-execute').disabled=false;updateGlobal();}
}
function pendingPose(){
 if(app.waistPending||app.waistError)throw new Error(app.waistError||'正在检查升降目标');
 const names=[...app.dirty].filter(n=>!n.startsWith('gripper_')&&!(app.waistIntent!==null&&liftNames.includes(n)));
 const targets=names.length?readTargets(names):{},grippers={};for(const side of ['left','right']){const n='gripper_'+side;if(app.dirty.has(n)){const v=Number(app.draft[n]);if(app.draft[n]===''||!Number.isFinite(v)||v<0||v>100)throw new Error('夹爪目标须在 0–100');grippers[side]=v;}}
 return {kind:'pose',targets,grippers,...(app.waistIntent!==null?{waist_delta_m:app.waistIntent}:{})};
}
let poseTimer;
function poseReviewObservation(){
 const s=app.state||{},activity=s.command_activity||{},jointTargets=[...app.dirty].some(n=>!n.startsWith('gripper_'))||app.waistIntent!==null;
 // Ground/held-joint checks also depend on unedited pose axes. Ignore sample
 // sequence and timestamps; only material changes should rerun geometry.
 const names=jointTargets?groupNames('pose'):[],positions={},freshness={},regions={};
 for(const n of names){const entry=s.joints?.[n],value=entry?.position,bounds=app.robot?.joint_limits_deg?.[n];positions[n]=finite(value)?value:null;freshness[n]=fresh(entry,app.robot?.feedback_max_age_s??.5);regions[n]=finite(value)&&bounds?(value<bounds[0]?-1:value>bounds[1]?1:0):null;}
 for(const side of ['left','right'])if(app.dirty.has('gripper_'+side)){const n='gripper_'+side,entry=s.grippers?.[side];positions[n]=finite(entry?.position)?entry.position:null;freshness[n]=fresh(entry,app.robot?.feedback_max_age_s??.5);}
 const context=JSON.stringify({connected:s.connected,error:s.error,authenticated:s.authenticated,boot:s.boot_id??s.system_health?.boot_id,session:s.session_id,action:s.action?[s.action.id,s.action.status]:null,control:[s.control?.owner,s.control?.reason,s.control?.stopping],capabilities:s.capabilities,activity:[activity.ready,activity.error,activity.active],graphFresh:finite(s.graph_age_s)&&s.graph_age_s<=(app.robot?.graph_max_age_s??3),freshness,regions});
 return {context,positions};
}
function samePoseReviewState(a,b){
 return Boolean(a&&b&&a.context===b.context&&Object.keys(a.positions).length===Object.keys(b.positions).length&&Object.entries(a.positions).every(([n,v])=>v===null?b.positions[n]===null:finite(b.positions[n])&&Math.abs(v-b.positions[n])<(n.startsWith('gripper_')?.5:.25)));
}
function maybeRefreshPoseReview(){
 if(app.page!=='control'||app.group==='base'||poseBusy()||actionRunning(app.state?.action)||app.waistPending||(!app.dirty.size&&app.waistIntent===null)||poseTimer||app.poseReviewInFlight!==null)return;
 if(samePoseReviewState(app.poseReviewState,poseReviewObservation()))return;
 app.poseReview=null;app.posePlan=null;app.poseError=null;app.poseSeq++;
 // At most one background review per two seconds, even while feedback moves.
 poseTimer=setTimeout(()=>{poseTimer=null;if(!poseBusy()&&!actionRunning(app.state?.action)&&!app.waistPending)refreshPosePlan();},Math.max(350,2000-(Date.now()-app.poseReviewStarted)));
}
function invalidatePose(){app.poseReviewState=null;app.poseNotice=null;app.poseReview=null;app.posePlan=null;app.poseError=null;app.poseSeq++;clearTimeout(poseTimer);updatePoseSummary();poseTimer=setTimeout(()=>{poseTimer=null;if(!poseBusy()&&!actionRunning(app.state?.action))refreshPosePlan();},350);}
async function refreshPosePlan(){
 if(app.group==='base')return;const seq=app.poseSeq;
 try{
  const spec=pendingPose();if(!Object.keys(spec.targets).length&&!Object.keys(spec.grippers).length&&spec.waist_delta_m===undefined){app.posePlan=null;app.poseReview=null;updatePoseSummary();return;}
  app.poseReviewState=poseReviewObservation();app.poseReviewStarted=Date.now();app.poseReviewInFlight=seq;
  const r=await api('/api/pose/review?spec='+encodeURIComponent(JSON.stringify(spec)),undefined,{timeout:15000});if(seq!==app.poseSeq)return;
  app.poseReview=r.review;app.posePlan=r.review.executable?r.review.plan:null;if(app.posePlan)headBoundaryPlan(app.posePlan);app.poseError=r.review.executable?null:(r.review.issues||[]).map(x=>humanError(x.message)).join('\n');
 }catch(e){if(seq!==app.poseSeq)return;app.poseReview=null;app.poseError=e.message;app.posePlan=null;}
 finally{if(app.poseReviewInFlight===seq)app.poseReviewInFlight=null;}
 updatePoseSummary();
}
function completedPoseStillCurrent(action,context){
 if(!verifiedAction(action)||!context||!app.apiOnline||!app.state?.connected)return false;
 const targets=context.targets||{},grippers=context.grippers||{};
 if(!Object.keys(targets).length&&!Object.keys(grippers).length)return false;
 return Object.entries(targets).every(([n,v])=>fresh(app.state.joints?.[n])&&Math.abs(app.state.joints[n].position-v)<=(app.robot?.joint_tolerance_deg??1))&&Object.entries(grippers).every(([side,v])=>fresh(app.state.grippers?.[side])&&Math.abs(app.state.grippers[side].position-v)<=(app.robot?.gripper_tolerance??5));
}
function updatePoseSummary(){
 const title=$('#pose-scope'),note=$('#pose-plan-summary'),status=$('#action-status');if(!title)return;
 if(app.group==='base'){title.textContent='底盘移动';note.textContent='按住移动，松开停止';if(status)status.innerHTML='';return;}
 const labels=[];for(const [prefix,label] of [['arm_l_','左臂'],['arm_r_','右臂'],['body_','身体'],['head_','头部'],['gripper_left','左夹爪'],['gripper_right','右夹爪']])if([...app.dirty].some(n=>n.startsWith(prefix)))labels.push(label);
 const current=app.activeAction,context=app.actionContext?.id===current?.id?app.actionContext:null,p=app.posePlan;
 const currentName=context?.title||'姿态调整',estimate=context?.estimatedTotal??estimatedTime(p),running=actionRunning(current);
 const error=app.poseError||current?.error||'',atTarget=completedPoseStillCurrent(current,context),external=app.state?.action?.id!==current?.id?app.state?.action:null;
 title.textContent=running?`正在${currentName==='全身复位'||currentName==='双臂复位'?'复位':'调整姿态'}`:atTarget?`${currentName} · 已到位`:verifiedAction(current)?`上次${currentName}已完成`:labels.length?pendingTitle():'调整姿态';
 let summary=error?briefPoseError(error,current?.result?.commands_sent===true):running?`${finite(estimate)?`预计 ${fmt(estimate,1)} 秒 · `:''}${context?.headBoundaryRecovery?'正在执行':'正在执行，可随时停止'}`:atTarget?'所有目标已确认到位':verifiedAction(current)?'当前姿态已变化或反馈待更新，请以实测为准':current?.status==='succeeded'?'执行已结束，尚未确认到位':current&&['failed','rejected','cancelled','stopped','completed'].includes(current.status)?`${actionLabel(current.status)}${current.result?.commands_sent?'，再次执行将从实测位置重新规划':'，目标保留'}`:p?.no_motion?'目标与当前姿态接近，执行后确认到位':p?`${labels.join('、')} · 预计 ${fmt(estimatedTime(p),1)} 秒`:labels.length?'正在检查目标…':app.poseNotice||'调整关节，或选择右侧姿态';
 if(!error&&!running&&p?.head_boundary_recovery)summary='头部需先回到工作范围 · 执行前需现场确认';
 if(running&&headBoundaryPhase(current))summary=current.status==='stopping'?'正在取消后续动作；已接收的头部边界目标无法撤回':'正在回到工作范围';
 else if(running&&current.status==='stopping')summary='正在停止，等待机器人反馈';
 if(current&&['cancelled','stopped'].includes(current.status)&&(context?.headBoundaryRecovery||current.result?.head_boundary_recovery))summary='后续动作已取消；已接收的头部边界目标无法撤回，不代表头部已停稳。';
 if(!running&&actionRunning(external)){title.textContent='其他会话正在执行';summary='当前仅观察，等待动作结束后再执行目标';}
 note.textContent=summary;note.classList.toggle('plan-error',Boolean(error)&&!actionRunning(external));
 if(status){const expanded=Boolean($('details[open]',status)),details=error?humanError(error):current?.result?JSON.stringify(current.result,null,2):'';status.innerHTML=`${running?`<progress aria-label="整项动作进度" value="${Math.max(0,Math.min(.99,Number(current.progress)||0))}" max="1"></progress>`:''}${details?`<details class="pose-diagnostics" ${expanded?'open':''}><summary>${error?'查看原因':'执行详情'}</summary><pre>${esc(details)}</pre></details>`:''}`;}
}

async function executeCurrent(){
 if(poseBusy())return;
 if(app.group==='base')return previewAction({kind:'base_authorize'});
 clearTimeout(poseTimer);poseTimer=null;app.poseSeq++;app.preflighting=true;updateGlobal();
 try{app.poseError=null;await refreshPosePlan();if(app.poseError)throw new Error(app.poseError);if(!app.posePlan)throw new Error('请先调整姿态或载入预设');
 return await previewAction(pendingPose());}finally{app.preflighting=false;updateGlobal();}
}

async function executeAction(action){
 app.submittedDraft={...app.draft};const title=pendingTitle(),estimatedTotal=estimatedTime(app.posePlan);
 const result=await api('/api/actions',{...action,lease_id:app.lease});app.activeAction=result.action;app.actionContext={id:result.action?.id,title,estimatedTotal,targets:{...(app.posePlan?.targets||{}),...(action.waist_delta_m!==undefined?app.waistPreview?.targets:{}),...(action.targets||{})},grippers:{...(action.grippers||{})},headBoundaryRecovery:action.head_boundary_recovery_ack===true};app.poseNotice=null;
 finishPoseDraft();updateControl();await refreshState();
}
function finishPoseDraft(){
 const action=app.activeAction;if(!verifiedAction(action)||!app.submittedDraft)return;
 for(const n of [...app.dirty])if(app.draft[n]===app.submittedDraft[n])app.dirty.delete(n);
 app.submittedDraft=null;app.poseReview=null;app.waistIntent=null;app.waistPreview=null;app.waistDelta=0;app.waistPending=false;app.waistError=null;app.posePlan=null;app.poseError=null;app.poseSeq++;app.activePresetId=null;seedLiveTargets();updatePoseSummary();renderPresets();
}
async function pollAction(){
 const current=app.activeAction;if(!current?.id||!actionRunning(current))return;
 try{const result=await api(`/api/actions/${encodeURIComponent(current.id)}`);if(app.activeAction?.id!==current.id)return;app.activeAction=result.action;finishPoseDraft();updateControl();updateGlobal();}catch{}
}

const jogTargets={forward:{vx:.05,vy:0,wz:0},back:{vx:-.05,vy:0,wz:0},left:{vx:0,vy:.05,wz:0},right:{vx:0,vy:-.05,wz:0},'turn-left':{vx:0,vy:0,wz:.1},'turn-right':{vx:0,vy:0,wz:-.1}};
function startJog(direction,button){if(poseBusy()||!canMove()||app.jog||app.stopping)return;app.jog={direction,lease_id:app.lease};button.classList.add('active');const send=()=>{if(!app.jog||app.jogPending)return;const jog=app.jog;app.jogPending=api('/api/actions',{kind:'base_jog',targets:jogTargets[jog.direction],ttl_s:.25,lease_id:jog.lease_id},{timeout:1500}).catch(error=>{toast(error.message,true);setTimeout(()=>stopJog(),0);}).finally(()=>{app.jogPending=null;});};send();app.jogTimer=setInterval(send,150);app.jogDeadlineTimer=setTimeout(()=>{stopJog();toast('本次点动已到 6 秒，请松手后重新按下');},6000);}
async function stopJog(){if(!app.jog)return;const lease=app.jog.lease_id;app.jog=null;clearInterval(app.jogTimer);clearTimeout(app.jogDeadlineTimer);app.jogTimer=null;app.stopping=true;$$('[data-jog]').forEach(el=>el.classList.remove('active'));const pending=app.jogPending;try{await api('/api/stop',{lease_id:lease},{timeout:1500,keepalive:true});if(pending){await pending;await api('/api/stop',{lease_id:lease},{timeout:1500,keepalive:true});}}catch{}finally{app.stopping=false;}}
async function stop(){await stopJog();if(!app.lease)throw new Error('当前浏览器没有可停止的工作台控制权');const result=await api('/api/stop',{lease_id:app.lease});if(result.action)app.activeAction=result.action;updateControl();await refreshState();}
async function savePreset(capture){const name=app.presetName.trim();if(!name)throw new Error('请先填写姿态名称');const body={name,capture};if(capture){body.joint_names=groupNames();if(!body.joint_names.length)throw new Error('请先选择双臂、头部或身体关节组');}else{const names=[...new Set([...groupNames(),...[...app.dirty].filter(key=>!key.startsWith('gripper_'))])];if(!names.length)throw new Error('请在双臂、头部或身体页保存关节姿态；可同时包含已编辑的夹爪目标');body.joints=readTargets(names);const grippers={};for(const side of ['left','right']){const raw=app.draft[`gripper_${side}`];if(app.dirty.has(`gripper_${side}`)&&raw!==undefined&&raw!==''){const value=Number(raw);if(!Number.isFinite(value)||value<0||value>100)throw new Error('夹爪目标必须为0–100');grippers[side]=value;}}if(Object.keys(grippers).length)body.grippers=grippers;}await api('/api/presets',body);await refreshPresets();toast(capture?'已保存当前实测姿态':'已保存编辑目标');}
function exportPresets(index){
 const rows=Number.isInteger(index)?[app.presets[index]]:app.presets;if(!rows.length||!rows[0])throw new Error('暂无可导出的姿态');
 const data={schema:'f1-console-presets-v1',robot_id:app.robot?.id,presets:rows};const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download=`${(rows.length===1?rows[0].name:'F1-presets').replace(/[\\/:*?"<>|]/g,'_')}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function newPreset(){
 let dialog=$('#new-preset-dialog');if(!dialog){dialog=document.createElement('dialog');dialog.id='new-preset-dialog';dialog.className='execute-dialog';dialog.innerHTML='<form id="new-preset-form"><h2>新增姿态</h2><div class="preset-buttons mt"><button type="button" class="button primary" id="choose-capture">保存当前姿态</button><button type="button" class="button" id="choose-import">导入 JSON</button></div><div id="capture-fields" hidden><p class="section-note">保存双臂、身体与头部的新鲜实测值。保存不会触发动作。</p><label class="field-label mt" for="new-preset-name">姿态名称</label><input id="new-preset-name" maxlength="80" style="width:100%"><p id="preset-error" role="alert"></p><button type="submit" class="button primary mt">保存当前姿态</button></div><div class="dialog-actions"><button type="button" class="button" id="close-new-preset">取消</button></div></form>';document.body.append(dialog);
 $('#close-new-preset').onclick=()=>dialog.close();$('#choose-capture').onclick=()=>{$('#capture-fields').hidden=false;$('#new-preset-name').focus();};$('#choose-import').onclick=()=>{dialog.close();$('#preset-file').click();};
 $('#new-preset-form').onsubmit=async event=>{event.preventDefault();const submit=$('button[type=submit]',dialog);submit.disabled=true;try{const name=$('#new-preset-name').value.trim();if(!name)throw new Error('请填写姿态名称');if(!await login())return;await api('/api/presets',{name,capture:true,joint_names:Object.values(groups()).flat()});await refreshPresets();dialog.close();toast('已保存当前实测姿态');}catch(error){$('#preset-error').textContent=error.message;}finally{submit.disabled=false;}};
 }
 $('#capture-fields').hidden=true;$('#preset-error').textContent='';$('#new-preset-name').value='';dialog.showModal();
}
async function deletePreset(index){const preset=app.presets[index];if(!preset)return;if(!confirm(`删除姿态「${preset.name}」？此操作只删除预设，不会移动机器人。`))return;if(!await login())return;await api('/api/presets/delete',{id:preset.id});await refreshPresets();toast('姿态已删除');}
const clicks={'data-select-start':()=>{dataSelecting=true;renderDataCatalog();},'data-select-cancel':()=>{resetDataSelection();renderDataCatalog();},'data-back':returnDataLibrary,'data-select-all':()=>{const leaves=libraryView(app.data,app.dataFolder||'').leaves,rows=dataLibraryPage().rows.filter(e=>deletable(e)&&leaves.some(leaf=>leaf.id===e.id));if(rows.every(e=>dataSelection.has(e.id)))for(const e of rows)dataSelection.delete(e.id);else for(const e of rows){if(dataSelection.size>=100)break;dataSelection.set(e.id,{id:e.id,source_signature:e.source_signature,name:e.name});}renderDataCatalog();},'data-delete':deleteSelectedData,'pico-recording':()=>$('#recorder-card')?.scrollIntoView({behavior:'smooth',block:'start'}),stop,'execute-current':executeCurrent,'focus-model':()=>{const focused=document.body.classList.toggle('model-focus');const button=$('[data-click="focus-model"]');if(button)button.textContent=focused?'返回调姿':'放大模型';app.viewer?.resize();},'toggle-nav':()=>{document.body.classList.toggle('nav-collapsed');try{localStorage.setItem('f1-nav-collapsed',document.body.classList.contains('nav-collapsed')?'1':'0');}catch{}$('#toggle-navigation').setAttribute('aria-expanded',String(!document.body.classList.contains('nav-collapsed')));app.viewer?.resize();},'startup':startBase,'new-preset':newPreset,login,acquire,release,'execute-joints':()=>previewAction({kind:'joints',targets:readTargets(groupNames())}),'execute-lift':()=>{if(app.liftPercent===null)throw new Error('请先拖动升降滑条或选择快捷位置');return previewAction(app.liftLevel?{kind:'lift',level:app.liftLevel}:{kind:'joints',targets:readTargets(liftNames)});},align:()=>{app.waistPending=false;app.waistError=null;app.waistIntent=null;app.waistPreview=null;app.waistDelta=0;app.waistPreviewSeq++;app.activePresetId=null;app.dirty.clear();app.activeAction=null;app.actionContext=null;seedLiveTargets();invalidatePose();persist();renderControlEditor();renderPresets();},capture:()=>savePreset(true),'save-draft':()=>savePreset(false),import:()=>$('#preset-file').click(),export:exportPresets,'refresh-data':refreshData,refresh:async()=>{await refreshState();await refreshSystem();toast('已刷新');},logout:async()=>{await release();await api('/api/logout',{});app.authenticated=false;updateGlobal();updateSystem();toast('已退出操作会话');}};
document.addEventListener('click',event=>{
 const wm=event.target.closest('[data-waist-mode]');if(wm){app.waistMode=wm.dataset.waistMode;app.waistPreview=null;app.waistPreviewSeq++;renderControlEditor();return;}
 const step=event.target.closest('[data-waist-step]');if(step){editWaistDelta(step.dataset.waistStep);return;}
 const lift=event.target.closest('[data-lift-level]');if(lift){if(lift.disabled)return;editLiftPercent(presetPercent(Number(lift.dataset.liftLevel),app.robot?.joint_limits_deg));app.liftLevel=Number(lift.dataset.liftLevel);return;}
 const teleop=event.target.closest('[data-teleop]');if(teleop&&!teleop.disabled){run(()=>teleopAction(teleop.dataset.teleop,teleop.dataset.variant));return;}
 const recordingButton=event.target.closest('[data-recording]');if(recordingButton&&!recordingButton.disabled){run(()=>recordingAction(recordingButton.dataset.recording));return;}
 const selectedData=event.target.closest('[data-select-episode]');if(selectedData){toggleDataSelection(selectedData.dataset.selectEpisode);return;}
 const episode=event.target.closest('[data-episode]');if(episode){run(()=>selectEpisode(episode.dataset.episode));return;}
 const nav=event.target.closest('[data-nav]');if(nav){stopJog();location.hash=nav.dataset.nav;return;}
 const folder=event.target.closest('[data-folder]');if(folder){if(dataDeleting)return;resetDataSelection();dataFilters.page=1;app.dataFolder=folder.dataset.folder||null;renderDataCatalog();return;}
 const dataPageButton=event.target.closest('[data-data-page]');if(dataPageButton){if(dataDeleting||dataPageButton.disabled)return;dataFilters.page=Number(dataPageButton.dataset.dataPage);renderDataCatalog();return;}
 const dataKind=event.target.closest('[data-data-kind]');if(dataKind){if(dataDeleting)return;resetDataSelection();dataFilters.kind=dataKind.dataset.dataKind;dataFilters.page=1;renderDataCatalog();return;}
 const group=event.target.closest('[data-group]');if(group){stopJog();app.group=group.dataset.group;if(app.group==='lift'){app.liftLevel=null;app.liftPercent=null;app.waistDelta=0;app.waistPreview=null;app.waistPreviewSeq++;}persist();renderPage();return;}
 const button=event.target.closest('[data-click]');if(button&&!button.disabled){run(async()=>{button.disabled=true;try{await clicks[button.dataset.click]?.();}finally{button.disabled=false;updateGlobal();}});return;}
 const exp=event.target.closest('[data-preset-export]');if(exp){run(()=>exportPresets(Number(exp.dataset.presetExport)));return;}
 const del=event.target.closest('[data-preset-delete]');if(del&&!del.disabled&&!poseBusy()){run(()=>deletePreset(Number(del.dataset.presetDelete)));return;}
 const presetButton=event.target.closest('[data-preset-index]');if(presetButton&&!presetButton.disabled&&!poseBusy()){const preset=app.presets[Number(presetButton.dataset.presetIndex)];app.liftPercent=null;app.liftLevel=null;app.draft={...preset.joints};app.dirty=new Set(Object.keys(app.draft));for(const [side,value] of Object.entries(preset.grippers||{})){app.draft[`gripper_${side}`]=value;app.dirty.add(`gripper_${side}`);}app.activePresetId=preset.id;app.presetName=preset.name;app.group='pose';app.waistPending=false;app.waistError=null;app.waistIntent=null;app.waistPreview=null;app.waistPreviewSeq++;app.activeAction=null;invalidatePose();persist();renderPage();return;}
 const gripper=event.target.closest('[data-gripper-execute]');if(gripper&&!gripper.disabled)run(()=>{const side=gripper.dataset.gripperExecute,raw=app.draft[`gripper_${side}`],position=Number(raw);if(raw===''||raw===undefined||!Number.isFinite(position)||position<0||position>100)throw new Error('请输入 0–100 之间的夹爪位置');return previewAction({kind:'gripper',side,position});});
});
document.addEventListener('change',event=>{if(event.target.id==='whole-part'){app.wholePart=event.target.value;renderControlEditor();}});
document.addEventListener('input',event=>{if(event.target.id==='data-search'){dataFilters.query=event.target.value;dataFilters.page=1;renderDataCatalog();return;}if(event.target.hasAttribute('data-waist-delta')){editWaistDelta(event.target.value);return;}if(event.target.hasAttribute('data-lift-percent')){editLiftPercent(event.target.value);return;}const input=event.target,name=input.dataset.targetInput||input.dataset.targetSlider;if(name){if(poseBusy())return;clearPresetSelection();if(liftNames.includes(name)){app.waistPending=false;app.waistError=null;app.waistIntent=null;app.waistPreview=null;app.waistPreviewSeq++;if($('#waist-plan-status'))$('#waist-plan-status').textContent='使用当前身体关节目标';}app.draft[name]=input.value;app.dirty.add(name);app.activeAction=null;invalidatePose();}else if(input.id==='preset-name')app.presetName=input.value;else if(input.id==='review-notes'||input.id==='review-status'){app.reviewDirty=true;return;}else return;persist();updateControl();});
$('#stop-button').addEventListener('click',()=>run(stop));
$('#execute-form').addEventListener('submit',confirmExecution);
$('#cancel-execute').onclick=()=>$('#execute-dialog').close();
$('#execute-dialog').addEventListener('cancel',event=>{if(app.executing)event.preventDefault();});
$('#execute-dialog').addEventListener('close',()=>{$('#execute-code').value='';app.pendingAction=null;app.pendingHeadRecovery=null;updateGlobal();});
$('#preset-file').addEventListener('change',event=>run(async()=>{const file=event.target.files[0];if(!file)return;try{if(file.size>200*1024)throw new Error('姿态文件超过 200 KB，请检查是否选择正确');const parsed=JSON.parse(await file.text());const presets=Array.isArray(parsed)?parsed:parsed.presets;if(!Array.isArray(presets))throw new Error('文件必须包含 presets 数组');if(!await login())return;await api('/api/presets/import',{presets});await refreshPresets();toast('姿态导入完成');}finally{event.target.value='';}}));
window.addEventListener('hashchange',()=>{stopJog();renderPage();});window.addEventListener('blur',()=>stopJog());document.addEventListener('visibilitychange',()=>{if(document.hidden)stopJog();});
window.addEventListener('pagehide',()=>{stopJog();if(app.lease)fetch('/api/control/release',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-F1-Request':'1'},body:JSON.stringify({lease_id:app.lease}),keepalive:true}).catch(()=>{});});
async function checkRevision(){try{const result=await api('/api/dev/revision');if(app.revision!==null&&result.revision&&result.revision!==app.revision){persist();await stopJog();if(app.lease)await release();location.reload();return;}app.revision=result.revision??app.revision;if($('#revision-label'))$('#revision-label').textContent=String(app.revision||'未提供').slice(0,18);}catch{}}
async function init(){renderPage();const results=await Promise.allSettled([api('/api/robot'),api('/api/session')]);if(results[0].status==='fulfilled')app.robot=results[0].value.robot;if(results[1].status==='fulfilled')app.authenticated=Boolean(results[1].value.authenticated);renderPage();await refreshState();await Promise.allSettled([refreshPresets(),refreshEvents(),refreshSystem(),checkRevision(),refreshRecording(),refreshTeleop()]);setInterval(refreshState,750);setInterval(heartbeat,900);setInterval(refreshRecording,1500);setInterval(refreshTeleop,1000);setInterval(pollAction,750);setInterval(refreshEvents,3000);setInterval(()=>{if(app.page==='data'){refreshData();const eid=new URLSearchParams(location.hash.split('?')[1]||'').get('episode');if(/^[a-f0-9]{24}$/.test(eid||'')&&app.selectedEpisode!==eid)selectEpisode(eid);}},10000);setInterval(refreshSystem,10000);setInterval(()=>{if(app.page==='system')refreshStartup();},5000);setInterval(checkRevision,2000);}
try{document.body.classList.toggle('nav-collapsed',localStorage.getItem('f1-nav-collapsed')==='1');}catch{}
init();

document.addEventListener("click",event=>{const b=event.target.closest("[data-hil-copy]");if(b)hilPanel.copy(b.dataset.hilCopy);});
setInterval(()=>hilPanel.refresh(),200);

document.addEventListener('click',event=>{for(const kind of ['play','retry','review']){const b=event.target.closest(`[data-hil-${kind}]`);if(b)hilPanel.mediaAction(kind,b.getAttribute(`data-hil-${kind}`));}});

document.addEventListener('click',event=>{if(event.target.closest('[data-hil-finish]'))hilPanel.finish();});

document.addEventListener('change',event=>{if(event.target.id==='data-review-filter'){dataFilters.review=event.target.value;dataFilters.page=1;renderDataCatalog();}});

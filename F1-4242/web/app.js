import {captureIcon,connectionRows} from './capture-ui.js';
import {SynchronizedVideos} from './synced-video.js';
import {previewState,pendingVideoHTML,jointOverviewHTML,timestampNotesHTML} from './data-presentation.js';
import {overviewHTML,startupPresentation,drawerHTML,startupCommand,startupResultHTML,mountPicoWorkspace,selectPicoView,selectPicoVariant,picoViews} from './workbench.js';
import {copyText} from './clipboard.js';
import {readContext,writeContext,stateFresh} from './ui-context.js';
import {MotionReplay,motionReplayHTML} from './motion-replay.js';
import {DataOrganizationUI} from './data-organization.js';
import {DataNotesEditor} from './data-notes.js';
import {DataCovers} from './data-covers.js';
import {libraryView,folderPage,recordTime,rltAssessment,captureMode} from './data-library.js';
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
// Display precision only: never round the draft used for planning or execution.
function targetDisplay(name,value){
 if(value===null||value===undefined||value===''||!Number.isFinite(Number(value)))return '';
 const precision=name.startsWith('gripper_')?0:1;
 return Number(Number(value).toFixed(precision)).toFixed(precision);
}
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
const tabs = [ ['overview','总览','overview'], ['control','调试与姿态','control'], ['capture','采集','signal'], ['data','数据','data'] ];
const pageLabel=key=>tabs.find(row=>row[0]===(['hil','pico'].includes(key)?'capture':key))?.[1]||'总览';
function captureHeading(method){return `<div class="capture-heading"><div><h1>采集</h1><p>连接设备，开始采集，保存并回看。</p></div><div><span class="capture-device-label">采集设备</span><nav class="capture-methods" aria-label="采集设备">${[['hil','HIL 手柄','pad'],['pico','Pico 遥操','headset']].map(([id,label,glyph])=>`<a href="#${id}" class="${method===id?'active':''}" aria-current="${method===id?'page':'false'}">${captureIcon(glyph)}${label}${method===id?captureIcon('check'):''}</a>`).join('')}</nav></div></div>`;}
function sidebarContext(){
 const node=$('#sidebar-context')||Object.assign(document.createElement('div'),{id:'sidebar-context',className:'sidebar-context'});
 const capture=['hil','pico'].includes(app.page),data=app.page==='data',navigation=$('#navigation'),parent=navigation.querySelector(capture?'[data-nav=capture]':'[data-nav=data]');
 node.classList.toggle('workspace-subnav',capture||data);node.classList.toggle('capture-subnav',capture);node.classList.toggle('data-subnav',data);
 if(capture||data){parent.after(node);parent.setAttribute('aria-expanded','true');parent.setAttribute('aria-controls','sidebar-context');}
 else navigation.after(node);
 if(app.page==='data'){node.innerHTML='<section class="data-versions" id="data-version-tree"></section>';return;}
 if(['hil','pico'].includes(app.page)){const pico=app.page==='pico',route=new URLSearchParams(location.hash.split('?')[1]||'').get('route');node.innerHTML=`<div class="sidebar-label">${pico?'PICO 遥操':'HIL 手柄'}</div><nav class="context-links">${(pico?picoViews:[['live','运行现场'],['connect','连接与启动'],['teaching','操作教学'],['diagnostic','诊断']]).map(([id,label])=>`<button ${pico?'data-pico-view':'data-context-hil'}="${id}" ${!pico&&!route?'disabled':''}>${captureIcon(({live:'screen',connect:'link',teaching:'book',diagnostic:'pulse',record:'screen',help:'book'})[id])}<span>${label}</span></button>`).join('')}</nav>`;return;}
 node.replaceChildren();
}
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
    if(!response.ok || data.ok===false) { if(response.status===401){app.authenticated=false;updateGlobal();} throw Object.assign(new Error(humanError(data.error || `服务返回 ${response.status}`)),{status:response.status,code:data.code,payload:data}); }
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
function briefPoseError(message,started=false){
 const text=humanError(message).split('\n')[0];
 // Keep the rejected axis and measured value; a range check does not establish
 // that the physical joint cannot return or that its calibration is correct.
 const origin=text.match(/^((?:头部|身体|左臂|右臂) J\d+\s*实测 .*?°[，\s]*超出(?:配置|允许)范围 [^。，；]+)/);
 if(origin)return `${origin[1].replace(/超出(?:配置|允许)范围/,'超出命令范围')}；${started?'任务已中止':'整套动作未开始'}。`;
 return text;
}
function labelJoint(name) { const match=/^arm_([lr])_j(\d+)$/.exec(name);if(match)return `${match[1]==='l'?'左臂':'右臂'} J${match[2]}`;if(name.startsWith('head_j'))return `头部 J${name.slice(6)}`;if(name.startsWith('body_j'))return `身体 J${name.slice(6)}`;return name; }
function pill(label,type='neutral') {return `<span class="pill ${type}">${esc(label)}</span>`;}
function heading(kicker,title,desc,actions='') {return `<div class="page-heading"><div><div class="eyebrow">${kicker}</div><h1>${title}</h1><p>${desc}</p></div>${actions?`<div class="heading-actions">${actions}</div>`:''}</div>`;}
function cardHeader(title,desc='',suffix='') {return `<div class="card-header"><div><h2>${title}</h2>${desc?`<p>${desc}</p>`:''}</div>${suffix}</div>`;}
function empty(title,desc,ic='eye') {return `<div class="empty-state compact">${icon(ic)}<h3>${title}</h3><p>${desc}</p></div>`;}
function leasePanel() {return `<section class="lease-card" id="lease-panel"><div><h3 id="lease-heading">观察模式</h3><p id="lease-description">点击执行时取得控制权并提交目标。</p></div><div class="lease-buttons"><button class="button primary" data-click="acquire" id="acquire-button">取得控制权 ${icon('arrow')}</button><button class="button" data-click="release" id="release-button" hidden>释放控制权</button></div></section>`;}
const cameraPanel=cameraHTML;
let cameraPreview=null;
let motionReplay=null;
const openPoseSections=new Set();
let activePosePart="arms-section";
function selectPosePart(id){
 if(!["arms-section","body-section","head-section"].includes(id))return;activePosePart=id;
 for(const node of $$("#control-editor > .pose-section")){node.open=true;node.hidden=node.id!==id;node.setAttribute("role","tabpanel");node.setAttribute("aria-labelledby","tab-"+node.id);}
 for(const button of $$("[data-pose-part]")){const selected=button.dataset.posePart===id;button.setAttribute("aria-selected",String(selected));button.tabIndex=selected?0:-1;button.classList.toggle("active",selected);}
}
function rememberPoseSections(){for(const node of $$('#control-editor > .pose-section')){if(node.open)openPoseSections.add(node.id);else openPoseSections.delete(node.id);}}

function overviewPage() { return overviewHTML(heading,cameraPanel,icon); }
function controlPage() {
 return `<div class="pose-workspace-heading"><h1>调试与姿态</h1><div class="pose-navigation" role="tablist">${[['pose','调姿'],['base','移动']].map(([key,label])=>`<button class="tab ${app.group===key?'active':''}" role="tab" aria-selected="${app.group===key}" data-group="${key}">${label}</button>`).join('')}</div><section class="card pose-presets"><span class="pose-preset-label">预设</span><div class="preset-list" id="preset-list"></div><button class="button small" data-click="new-preset">${icon('plus')} 新增</button></section></div><div class="pose-control-status" id="pose-control-status" hidden><span>操控模式</span><div><button class="button small" data-click="stop">停止动作</button><button class="button small" data-click="release">退出操控</button></div></div><div id="pose-parts"></div><div class="control-layout"><section class="editor-card"><div id="capability-note" class="notice warn" hidden></div><div id="control-editor"></div></section><aside class="stack model-column"><section class="card model-card">${cardHeader('姿态预览', '', `<button class="button small" data-click="focus-model">放大模型</button>`)}<div id="robot-model" class="robot-model"></div><p id="model-ground-warning" class="notice warn" hidden></p><div class="model-legend"><span><i></i>实测</span><span><i class="target"></i>目标</span></div><details class="model-info"><summary>模型说明</summary><p>姿态参考；地面检查不覆盖自碰撞、环境或平衡。拖动旋转，滚轮缩放。</p></details></section>${cameraPanel('实时视野','')}</aside></div><div id="pose-actions"></div>`;
}
function picoPage() {
 return `${captureHeading('pico')}${cameraPanel()}<section class="card" id="teleop-session"></section><div class="grid two mt"><section class="card">${cardHeader('meaxiao · 上肢版','1.4.4 基线 / F1-09 upper-v2',pill('双臂精细操作'))}<p>左右臂独立 Grip 接管，启动对齐实测姿态，保留精调、滤波与轨迹输出。</p><div class="notice mt">关闭底盘、腰部、ABXY 复位和手柄录制。数采在本页独立启动，保存后到「数据」页查看。</div><div class="quick-links mt"><button class="button primary" data-teleop="prepare" data-variant="upper">启动上肢版</button><button class="button" data-click="pico-recording">打开数采</button></div><details class="mt"><summary>现场终端脚本</summary><h3>检查 / 启动 / 停止</h3><pre>cd ${esc((app.state?.pico?.start_script||'integrations/pico_upper/start.sh').replace(/\/start\.sh$/,''))}
./check.sh
./start.sh
# 结束本会话
./stop.sh</pre><p class="section-note">原终端启动流程需先运行 PCService；网页启动会复用已有服务，或启动自己的服务。</p></details></section><section class="card">${cardHeader('LXL · 原始流程','参考文档 v5 / 0804 · 部署版本以实机为准',pill('main 保持原样'))}<p>调用原 start_all_pico1.sh，保留原始初始姿态、夹爪、腰部和 MCAP 数采行为。</p><div class="notice warn mt">启动可能移动到配置初始姿态；X / Y 仍会升降腰部。这套不是上肢限制模式。</div><div class="quick-links mt"><button class="button" data-teleop="prepare" data-variant="lxl">启动 LXL</button></div><details class="mt"><summary>现场终端脚本</summary><pre>cd /home/user/lxl
./start_all_pico1.sh
# 原停止脚本
./stop_all_pico.sh</pre><p class="section-note">原停止脚本按名称结束遥操和 PCService；网页停止改用本会话的进程组，不直接执行这个批量停止脚本。</p></details></section><section class="card recorder-card span-two" id="recorder-card">${empty('正在读取独立录制器','此入口使用独立 LeRobot 上肢分支。','data')}</section><section class="card span-two">${cardHeader('操作与数采','两套按键逻辑不同，使用前核对版本')}<div class="table-scroll"><table class="data-table"><thead><tr><th>功能</th><th>LXL 原流程</th><th>meaxiao 上肢适配</th></tr></thead><tbody><tr><td>手臂接管</td><td>扳机接管，松开停止；再次接管基于当前姿态</td><td>左右 Grip 独立接管；松开 Grip 后退出该臂跟随</td></tr><tr><td>夹爪</td><td>按文档：扳机按下张开，松开闭合</td><td>接管后由扳机控制夹爪；未接管不改夹爪</td></tr><tr><td>A / B</td><td>开始 / 停止原 MCAP 数采</td><td>关闭；本页使用独立上肢录制</td></tr><tr><td>X / Y / XY</td><td>腰下降 / 上升（超过 0.3 秒）；XY 回配置初始位</td><td>全部关闭，不执行全身复位</td></tr><tr><td>保存位置</td><td>/home/user/lxl/pico_data/all_data/</td><td>${esc(app.recording?.output_root||'由后端配置')}；本地保存</td></tr><tr><td>采样</td><td>文档 v5 标称 30 Hz / 13 类数据，需实测验证</td><td>当前独立录制为 16D / ${finite(app.recording?.fps)?app.recording.fps:'—'} FPS，三路相机</td></tr></tbody></table></div><p class="section-note">Pico 与 F1 接同一局域网。在 Xrobotokit 选择 F1，启用 Head、Controller、Hand，再点击 Send。原 LXL 文档仅说明双臂与静态身体的碰撞检测，不保证动态双臂之间的碰撞保护。</p></section></div>`;
}
function layoutPico(){
 mountPicoWorkspace();selectPicoView(new URLSearchParams(location.hash.split('?')[1]||'').get('view')||'live',false);
}
function showPicoView(view){
 const recorder=$('#recorder-card');if(!['record','live'].includes(view)&&recorder)disposeMediaViewers(recorder);
 selectPicoView(view);if(['record','live'].includes(view)&&recorder)mountMediaViewers(recorder);cameraPreview?.sync();
}
function dataPage() {
 return `<div class="data-refinements">${heading('','数据','查看录制、任务结果与数据用途',`<button class="button" data-click="refresh-data">${icon('refresh')}刷新</button><details class="storage-popover"><summary id="data-storage-summary">存储空间</summary><div class="data-storage" id="data-storage" aria-label="本机数据存储概览"></div></details><div id="data-header-actions"></div>`)}<div id="data-library-view" class="data-list-view"><section class="card file-explorer data-library"><span id="episode-count" hidden>读取目录中…</span><div class="data-browse-toolbar"><div class="data-browse-mode" role="group" aria-label="录制浏览方式"><button class="button" data-browse-mode="folders" aria-pressed="${dataFilters.browseMode==='folders'}">按文件夹</button><button class="button" data-browse-mode="time" aria-pressed="${dataFilters.browseMode==='time'}">按时间</button></div><label class="data-folder-search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></svg><input id="data-search" type="search" value="${esc(dataFilters.query)}" placeholder="搜索录制或备注" aria-label="搜索录制或备注"></label></div><div class="data-library-body"><div class="folder-sections"><div id="data-folders"></div></div></div></section><details class="card mt data-storage-disclosure"><summary>录制与存储详情</summary><div id="data-status"></div></details></div><div id="episode-detail" class="mt" hidden></div></div>`;
}
function updateStartup(){
 const view=startupPresentation(app,groups());
 if($('#startup-title'))$('#startup-title').textContent=view.title;
 if($('#startup-summary'))$('#startup-summary').textContent=view.summary;
 $('.workbench-startup')?.classList.toggle('ready',view.ready);
 $$('[data-start-base]').forEach(b=>{b.disabled=!view.canStart||Boolean(app.startupConfirming);b.hidden=false;b.title=view.ready?'基础服务已就绪，无需重复启动':view.summary;});
 if($('#startup-detail-title'))$('#startup-detail-title').textContent=view.title;
 if($('#base-start-status'))$('#base-start-status').textContent=app.startupOnline?[app.startup?.message,app.startup?.reason].filter(Boolean).filter((v,i,a)=>a.indexOf(v)===i).join(' · '):'基础启动状态暂不可用';
 if($('#startup-result'))$('#startup-result').innerHTML=startupResultHTML(app);
 if($('#startup-command'))$('#startup-command').textContent=startupCommand(app.startup||{});
}
async function refreshStartup(reconcile=false){
 if(app.startupPollBusy)return;app.startupPollBusy=true;
 try{const r=await api('/api/startup/state');if(!r.startup)throw Error('启动状态缺失');app.startup=r.startup;app.startupOnline=true;
  if(['starting','completed','running','failed'].includes(app.startup.stage)||app.startup.existing_pids?.length||(reconcile&&app.startup.stage==='idle'&&app.startup.can_start===true))app.startupUncertain=false;
 }catch{app.startupOnline=false;}finally{app.startupPollBusy=false;updateStartup();}
}
async function startBase(){
 if(app.startupBusy||app.startupConfirming)return;app.startupConfirming=true;
 try{
  if(!await login())return;await refreshStartup();
  if(!startupPresentation(app,groups()).canStart)throw Error(app.startup?.reason||'启动状态不允许执行，请刷新检查');
  await refreshStartup();if(!startupPresentation(app,groups()).canStart)throw Error('启动条件已变化，请检查最新状态');
  app.startupBusy=true;updateStartup();
  try{const r=await api('/api/startup/start',{confirm:true});app.startupAccepted=r.startup?.accepted===true;app.startupUncertain=r.startup?.accepted!==true;toast(r.startup?.message||'启动请求已提交');}
  catch(error){app.startupUncertain=!error.status;throw error;}
  finally{app.startupBusy=false;await refreshStartup();}
 }finally{app.startupConfirming=false;updateStartup();}
}
function openWorkbenchPanel(kind,tab='identity'){
 if($('#entry-notice')?.open){app.deferredWorkbenchPanel={kind,tab};return;}
 let d=$('#workbench-drawer');if(!d){d=document.createElement('dialog');d.id='workbench-drawer';d.className='workbench-drawer';d.setAttribute('aria-labelledby','workbench-drawer-title');document.body.append(d);d.addEventListener('close',()=>{if(!d.open){app.workbenchPanel=null;d.replaceChildren();}});}
 app.workbenchPanel=kind;d.innerHTML=drawerHTML(kind,tab,app);if(!d.open)d.showModal();updateSystem();updateStartup();refreshSystem();if(kind==='startup')refreshStartup();
}
async function copyWorkbench(command=false){
 const text=command?startupCommand(app.startup||{}):$('#workbench-drawer')?.innerText||'';
 if(await copyText(text)){toast('已复制');return;}
 const node=command?$('#startup-command'):$('#workbench-drawer');if(node){const range=document.createRange();range.selectNodeContents(node);const selection=getSelection();selection.removeAllRanges();selection.addRange(range);}toast('浏览器未允许复制，已选中文本，可按 Ctrl+C');
}
const hilPanel=new HILPanel({api,esc,toast,login,getRobot:()=>app.robot,getConnection:()=>({online:app.apiOnline&&Date.now()-app.lastReceived<3000,connected:app.state?.connected})});
function hilPage(){return captureHeading('hil')+hilPanel.html();}
const pages={hil:hilPage,overview:overviewPage,control:controlPage,pico:picoPage,data:dataPage};
function renderPage() {
  dataNavigation++;rememberPoseSections();
  let key=location.hash.slice(1).split('?')[0];
  if(key==='capture'){key='hil';history.replaceState(null,'','#hil');}
  if(['experiments','demos','demo'].includes(key)){key='control';app.group='pose';persist();history.replaceState(null,'','#control');}
  const legacySystem=key==='system';if(legacySystem){key='overview';history.replaceState(null,'','#overview');}
  const restorePanel=legacySystem?'system':app.page===key?app.workbenchPanel:null;
  $('#workbench-drawer')?.close();
  app.page=pages[key]?key:'overview';document.body.dataset.page=app.page;
  $('#navigation').innerHTML=tabs.map(([name,label,ic],i)=>`<button class="nav-item ${(app.page===name||(name==='capture'&&['hil','pico'].includes(app.page)))?'active':''}" data-nav="${name}" aria-label="${esc(label)}" title="${esc(label)}" ${(app.page===name||(name==='capture'&&['hil','pico'].includes(app.page)))?'aria-current="page"':''}>${icon(ic)}<span>${label}</span><span class="nav-index">0${i+1}</span></button>`).join('');
  $('#breadcrumb-title').textContent=pageLabel(app.page);
  cameraPreview?.dispose();cameraPreview=null;hilPanel.dispose();disposeMediaViewers();app.viewer?.dispose();app.viewer=null;app.viewerGeneration++;$('#main').innerHTML=pages[app.page]();sidebarContext();document.title=`${pageLabel(app.page)} · F1 4242`;
  if(app.page==='control'){renderControlEditor();renderPresets();mountRobotViewer();}if(app.page==='data'){const eid=new URLSearchParams(location.hash.split('?')[1]||'').get('episode');if(/^[a-f0-9]{24}$/.test(eid||'')&&app.selectedEpisode!==eid)selectEpisode(eid);else if(eid&&app.selectedEpisode&&!app.episodeDetail)selectEpisode(app.selectedEpisode);else {if(!eid){app.selectedEpisode=null;app.episodeDetail=null;app.dataDetailIssue=null;}renderDataCatalog();if(!eid)restoreDataPosition();}refreshData();}
  if(app.page==='pico')layoutPico();
  const preview=document.querySelector('[data-camera-preview]');if(preview)cameraPreview=new CameraPreview(preview,{fps:app.page==='hil'?10:4/3,openOnMount:app.page==='pico'});
  if(app.page==='hil'){hilPanel.mount();hilPanel.refresh();}
  if(app.page==='pico'){renderRecording();refreshRecording();}
  updatePanels();updateGlobal();if(app.page==='overview')refreshStartup();if(restorePanel)openWorkbenchPanel(restorePanel);
}
function poseToolbar(){return `${app.replayReturn?`<a class="button small" href="#data?episode=${encodeURIComponent(app.replayReturn)}">返回 Replay 准备</a>`:''}<div class="pose-toolbar"><div class="pose-intent"><small id="pose-draft-scope"></small><strong id="pose-scope">请选择目标</strong><p id="pose-plan-summary" aria-live="polite">选择关节目标，或载入上方预设</p></div><div class="pose-toolbar-actions"><button class="button" data-click="align" title="放弃全部待执行目标">${icon('refresh')} 重置编辑</button><button class="button primary" data-click="execute-current" data-execute-required>执行 ${icon('arrow')}</button></div><div id="action-status" aria-live="polite"></div></div>`;}
function renderControlEditor(){
 const holder=$('#control-editor');if(!holder)return;rememberPoseSections();
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
  holder.innerHTML=`<details class="pose-section" id="arms-section"><summary>双臂</summary><div class="arm-comparison-heading"><strong>左臂</strong><strong>右臂</strong></div><div class="arm-comparison">${Array.from({length:7},(_,i)=>`<div class="arm-pair">${editor('arm_l_j'+(i+1),'J'+(i+1))}${editor('arm_r_j'+(i+1),'J'+(i+1))}</div>`).join('')}<div class="arm-pair">${grip('left')}${grip('right')}</div></div></details><details class="pose-section" id="body-section"><summary>身体</summary><div class="body-height"><h3>升降</h3><div class="slider-field height-slider"><input type="range" min="-10" max="10" step=".1" value="0" data-waist-delta aria-label="模型相对升降滑块"></div><div class="height-control"><button class="button" data-waist-step="-2">下降 2 cm</button><label><input id="waist-delta" type="number" min="-10" max="10" step=".1" value="0" data-waist-delta aria-label="保持俯仰的模型相对高度"> cm</label><button class="button" data-waist-step="2">上升 2 cm</button></div><p id="waist-plan-status" class="section-note">保持当前弯腰角度 · 模型相对位移</p></div>${editor('body_j1','转身')}${editor('body_j2','侧倾')}${editor('body_j3','弯腰')}<details class="advanced-joints"><summary>高级关节调整</summary>${['body_j4','body_j5'].map((n,i)=>editor(n,'身体 J'+(i+4))).join('')}<p>修改关节角度会替代当前升降目标。</p></details></details><details class="pose-section" id="head-section"><summary>头部</summary>${groups().head.map((n,i)=>editor(n,'J'+(i+1))).join('')}</details><details class="motion-info"><summary>控制说明</summary><p>选择目标后点击执行，系统协调所有待调整部位。保持固定速度，预计用时显示在顶部。保留关节行程与模型地面检查，尚无完整碰撞规划。</p></details>`;
 }
 if(app.group!=='base'){
  const columns='<div class="joint-column-labels"><span>关节 / 实际</span><span>目标</span></div>';
  for(const title of $$('.arm-comparison-heading strong',holder)){
   const column=document.createElement('div');title.replaceWith(column);column.append(title);column.insertAdjacentHTML('beforeend',columns);
  }
  for(const id of ['body-section','head-section']){
   const first=$('.joint-editor',document.getElementById(id));first?.insertAdjacentHTML('beforebegin',columns);
  }
 }
 for(const node of $$('.pose-section',holder)){node.open=openPoseSections.has(node.id);node.addEventListener('toggle',()=>{if(!node.isConnected)return;if(node.open)openPoseSections.add(node.id);else openPoseSections.delete(node.id);});}
 if(app.group!=='base')holder.insertAdjacentHTML('afterbegin',`<div class="pose-part-tabs" role="tablist" aria-label="调整部位">${[['arms-section','双臂与夹爪'],['body-section','身体'],['head-section','头部']].map(([id,label])=>`<button class="tab" id="tab-${id}" role="tab" aria-controls="${id}" data-pose-part="${id}">${label}<span data-part-dirty="${id}"></span></button>`).join('')}</div>`);
 $('#pose-actions').innerHTML=poseToolbar();$('#pose-parts').replaceChildren();const partTabs=holder.querySelector('.pose-part-tabs');if(partTabs)$('#pose-parts').append(partTabs);selectPosePart(activePosePart);updateControl();updateGlobal();updatePoseSummary();
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
 return `<div class="target-control"><div class="slider-field"><div class="range-limits"><span>${valid?esc(limits[0]):'—'}</span><span>${valid?esc(limits[1]):'—'}</span></div><input type="range" ${valid?`min="${limits[0]}" max="${limits[1]}"`:'disabled'} step="${step}" data-target-slider="${esc(name)}" aria-label="${esc(label)}目标滑块" ${value!==''?`value="${esc(value)}"`:'disabled'}></div><div class="number-field"><input id="target-${esc(name)}" type="number" ${valid?`min="${limits[0]}" max="${limits[1]}"`:''} step="any" data-target-input="${esc(name)}" aria-label="${esc(label)}目标" placeholder="等待实测" value="${esc(targetDisplay(name,value))}"><span>${name.startsWith('gripper_')?'%':'°'}</span></div></div>`;
}
function seedLiveTargets() {
 for(const [name,entry] of Object.entries(app.state?.joints||{}))if(!app.dirty.has(name)&&fresh(entry))app.draft[name]=Math.round(entry.position*1000)/1000;
 for(const side of ['left','right']){const name=`gripper_${side}`,entry=app.state?.grippers?.[side];if(!app.dirty.has(name)&&fresh(entry))app.draft[name]=Math.round(entry.position*1000)/1000;}
}
function syncTargetInputs() {
 for(const el of $$('[data-target-input],[data-target-slider]')){
  const name=el.dataset.targetInput||el.dataset.targetSlider,value=app.draft[name];
  if(document.activeElement!==el)el.value=el.dataset.targetInput?targetDisplay(name,value):value??'';
  if(el.dataset.targetSlider){const limits=name.startsWith('gripper_')?[0,100]:app.robot?.limits?.[name]||app.robot?.joint_limits_deg?.[name];el.disabled=poseBusy()||!Array.isArray(limits)||value===undefined||value==='';if(Array.isArray(limits)&&finite(Number(value)))el.style.setProperty('--range-fill',`${Math.max(0,Math.min(100,(Number(value)-limits[0])/(limits[1]-limits[0])*100))}%`);}
 }
 $$('[data-editor-row]').forEach(el=>el.classList.toggle('edited',app.dirty.has(el.dataset.editorRow)));
 for(const badge of $$('[data-part-dirty]')){const section=document.getElementById(badge.dataset.partDirty);const n=section?$$('[data-editor-row]',section).filter(el=>app.dirty.has(el.dataset.editorRow)).length:0;badge.textContent=n?' · '+n+' 项已改':'';}

}
async function mountRobotViewer(){
 const generation=app.viewerGeneration,container=$('#robot-model');if(!container||$('#entry-notice')?.open)return;
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
 health[health.length-1][2]=app.teleopOnline?(['idle','stopped'].includes(app.teleop?.phase)?{text:'未运行',type:'neutral',desc:'可选遥操会话'}:statusFor(s.pico,'service')):{text:'状态待确认',type:'neutral',desc:'未取得最新遥操状态'};
 const faults=health.filter(row=>row[0]!=='Pico'&&row[2].type!=='good'),ordered=[...health];$('#health-summary').textContent=`${health.slice(0,-1).filter(row=>row[2].type==='good').length}/${health.length-1} 项反馈正常`;$('#overview-alerts').innerHTML=faults.slice(0,4).map(([label,ic,status])=>`<button type="button" class="workbench-status-link" data-click="system-details">${esc(label)} · ${esc(status.text)}</button>`).join('');
 $('#health-grid').innerHTML=ordered.map(([label,ic,status])=>`<div class="health-item" title="${esc(status.desc)}"><div class="health-top"><i class="device-dot ${status.type}" aria-hidden="true"></i><span>${label}</span></div><span class="health-status">${esc(status.text)}</span><p>${esc(status.desc)}</p></div>`).join('');
 $('#joint-overview').innerHTML=[['left_arm','左臂'],['right_arm','右臂']].map(([key,label])=>`<div><div class="feedback-column-header"><strong>${label}</strong><span>ACTUAL / DEG</span></div>${(g[key]||[]).map((name,index)=>{const entry=s.joints?.[name],value=entry?.position,width=finite(value)?Math.min(Math.abs(value)/180*50,50):0;return `<div class="joint-mini ${entry&&!fresh(entry)?'stale':''}"><span>J${index+1}</span><div class="meter"><div class="meter-fill" style="left:${finite(value)&&value<0?50-width:50}%;width:${width}%"></div></div><output>${fmt(value)}${finite(value)?'°':''}</output></div>`;}).join('')}</div>`).join('');
 const active=s.action||app.activeAction;
 $('#work-status').innerHTML=lineList([['控制者',app.lease?'当前浏览器':s.control?.owner||'无已知控制者'],['操作会话',app.authenticated?'已启用':'只读观察'],['机器人反馈',app.apiOnline?(s.connected?'已连接':'未连接'):'状态失联'],['当前动作',active?`${actionLabel(active.status)}`:'无工作台动作']]);
 const h=app.hilOverview,hilActive=h?.active&&h.summary_fresh&&stateFresh(h,app.hilOverviewReceived,true,'summary_age_s',h.source==='structured_snapshot'?2:8),teleopActive=app.teleopOnline&&app.teleop&&stateFresh(app.teleop,app.teleopReceived,true)&&!['idle','failed','stopped'].includes(app.teleop.phase);const task=active&&actionRunning(active)?[active.replay?'实机复现':'姿态任务',active.replay?`#data?episode=${active.replay.episode_id}`:'#control']:hilActive?['HIL 会话','#hil']:teleopActive?['Pico 遥操','#pico']:['打开数据','#data'];$('#overview-task').innerHTML=`<a class="button" href="${esc(task[1])}">${esc(task[0])} →</a>`;
 $('#overview-current-task').textContent=!app.apiOnline?'状态待确认':active&&actionRunning(active)?actionLabel(active.status):hilActive?'HIL 会话运行中':teleopActive?'Pico 遥操运行中':'暂无运行任务';$('#overview-control-source').textContent=!app.apiOnline?'状态待确认':ownLease()?'当前浏览器':s.control?.owner||((hilActive||teleopActive)?'外部采集会话':'未接管');$('#overview-feedback').textContent=app.apiOnline?(s.connected?'已连接 · 时效见设备状态':'等待机器人反馈'):'连接中断';
 renderEvents($('#event-list'));updateStartup();
}
function lineList(rows){return rows.map(([label,value])=>`<div class="line-item"><span class="label">${esc(label)}</span><span class="value">${esc(value??'—')}</span></div>`).join('');}

function updateControl() {
 if(app.page!=='control')return;
 const s=app.state||{};syncTargetInputs();syncLiftControl();updateWaistPreview();updateRobotViewer();
 $$('[data-joint-live]').forEach(el=>{const entry=s.joints?.[el.dataset.jointLive];el.textContent=targetDisplay(el.dataset.jointLive,entry?.position)||'—';el.classList.toggle('stale',!app.apiOnline||!fresh(entry));el.title=finite(entry?.age_s)?`接收时间：${fmt(entry.age_s,2)} 秒前`:'尚未收到反馈';});
 $$('[data-gripper-live]').forEach(el=>{const entry=s.grippers?.[el.dataset.gripperLive];el.textContent=targetDisplay('gripper_'+el.dataset.gripperLive,entry?.position)||'—';el.classList.toggle('stale',!app.apiOnline||!fresh(entry));});
 if($('#base-feedback'))$('#base-feedback').innerHTML=lineList([['反馈时效',finite(s.base?.age_s)?`${fmt(s.base.age_s,2)} 秒前`:'未收到'],['实测平移速度',`${fmt(s.base?.vx,3)} / ${fmt(s.base?.vy,3)} m/s`],['实测转速',`${fmt(s.base?.wz,3)} rad/s`],['局部位置',`${fmt(s.base?.pose?.x,3)} / ${fmt(s.base?.pose?.y,3)} m`]]);

 updatePoseSummary();
 for(const [id,names] of [['arms-section',[...groups().left_arm,...groups().right_arm,'gripper_left','gripper_right']],['body-section',groups().body],['head-section',groups().head]]){const summary=$('#'+id+'>summary');if(summary){let count=names.filter(name=>app.dirty.has(name)).length;if(id==='body-section'&&app.waistIntent!==null)count++;let badge=summary.querySelector('[data-dirty-count]');if(!badge){badge=document.createElement('small');badge.dataset.dirtyCount='';summary.append(badge);}badge.textContent=count?`已修改 ${count} 项`:'';}}
 const presetSummary=$('#preset-summary');if(presetSummary)presetSummary.textContent=app.activePresetId?pendingTitle():`${app.presets.length} 个`;
}

function actionLabel(status){return ({queued:'等待执行',running:'执行中',completed:'命令执行结束',succeeded:'执行结束',failed:'执行失败',rejected:'执行被拒绝',cancelled:'已取消',stopped:'已停止',stopping:'正在停止',accepted:'已受理'})[status]||status||'等待状态';}
const teleopLabels={idle:'尚未启动',starting:'正在启动',preparing:'正在预检',awaiting_confirmation:'等待现场确认',waiting_xr:'等待 Pico 数据与手柄松开',workers_starting:'正在等待遥操就绪',running:'遥操进程就绪',stopping:'正在停止',stopped:'已停止',failed:'启动失败或会话失联'};
function updatePico() {
 const t=app.teleop,live=app.teleopOnline&&(!t?.id||stateFresh(t,app.teleopReceived,true)),active=t&&!['idle','stopped','failed'].includes(t.phase),label=teleopLabels[t?.phase]||'等待状态';
 const indicator=$('#teleop-indicator');if(indicator){indicator.hidden=!active&&!t?.can_stop;indicator.textContent=`● ${live?label:'遥操状态失联'}`;}
 if(app.page!=='pico')return;
 const holder=$('#teleop-session');if(!holder)return;
 const services=app.state?.services||{},external=['lxl_teleop','lxl_bridge','pico_upper'].filter(k=>services[k]?.running);
 const blockers=!active?['joint_command','left_gripper_command','right_gripper_command'].flatMap(k=>(app.state?.graph?.[k]?.publishers||[]).map(p=>p.name||'unknown')):[];
 const {checked_unix,log_tail,...viewState}=t||{};
 let feedback=$('#pico-request-error');if(!feedback){feedback=document.createElement('div');feedback.id='pico-request-error';feedback.className='notice warn mt';holder.after(feedback);}feedback.hidden=!app.teleopRequestError;feedback.replaceChildren();if(app.teleopRequestError){feedback.append(document.createTextNode(app.teleopRequestError+' '));const retry=document.createElement('button');retry.className='button small';retry.textContent='重新读取状态';retry.onclick=()=>refreshTeleop();feedback.append(retry);}
 const stopButton=$('#pico-persistent-stop');if(stopButton){stopButton.hidden=!active&&!t?.can_stop;stopButton.disabled=!app.authenticated||app.teleopBusy;stopButton.textContent=['preparing','awaiting_confirmation','starting'].includes(t?.phase)?'取消启动':'结束遥操';}
 const banner=$('#pico-persistent-state');if(banner){banner.hidden=!active&&!app.teleopRequestError&&!t?.error;banner.replaceChildren(document.createTextNode((live?label:'遥操状态失联')+(t?.error?' · '+t.error:'')));}
 const progress=$('#pico-connect-progress');if(progress){const rows=connectionRows([['基础反馈',app.apiOnline&&app.state?.connected?'已连接':'未确认',app.apiOnline&&app.state?.connected?'good':'','arm'],['Pico 数据',!live?'未确认':t?.phase==='waiting_xr'?'等待数据与手柄松开':t?.phase==='running'?'已通过启动检查':'待检查',live&&t?.phase==='running'?'good':'','headset'],['遥操会话',live?label:'状态失联',live&&t?.phase==='running'?'good':'','screen']]);progress.innerHTML=`<h2>连接进度</h2><h3>${live?label:'状态失联'}</h3>${rows}`;}
 const signature=JSON.stringify({t:viewState,online:live,auth:app.authenticated,busy:app.teleopBusy,requestError:app.teleopRequestError,lease:app.lease,external,blockers});
 if(holder.dataset.signature!==signature){holder.dataset.signature=signature;
 const config=t?.legacy_config||{},poses=['target_left','target_right','reset_left','reset_right'].map(k=>`<div class="line-item"><span>${({target_left:'启动左臂',target_right:'启动右臂',reset_left:'复位左臂',reset_right:'复位右臂'})[k]}</span><span class="mono">${esc(Array.isArray(config[k])?config[k].join(', ')+' °':'未读到配置')}</span></div>`).join('');
 holder.innerHTML=`${cardHeader('遥操会话',t?.variant==='lxl'?'LXL 原始流程':t?.variant==='upper'?'meaxiao 上肢版':'一次运行一套遥操',pill(live?label:'状态失联',live&&t?.phase==='running'?'good':live&&t?.phase==='failed'?'warn':'neutral'))}${!app.authenticated?'<button class="button" data-click="login">启用遥操操作</button>':''}${app.lease?'<div class="notice warn mt">先释放姿态控制权，再准备遥操。<button class="button small" data-click="release">释放控制权</button></div>':''}${!active&&external.length?'<div class="notice warn mt">检测到现场启动的遥操进程。请从原启动入口停止后再用网页；网页不会接管或结束外部会话。</div>':''}${blockers.length?`<div class="notice warn mt">Pico 原生预检仍要求独占：已有命令发布者 ${esc([...new Set(blockers)].join('、'))}。请从它们的原入口结束控制后重试。</div>`:''}${t?.error?`<div class="notice warn mt" role="alert">${esc(t.error)}</div>`:''}<div class="quick-links mt">${active||t?.can_stop?`<button class="button stop-button" data-teleop="stop" ${!app.authenticated||app.teleopBusy?'disabled':''}>${['preparing','awaiting_confirmation','starting'].includes(t.phase)?'取消本次启动':'停止本会话遥操'}</button>`:''}</div><details class="mt"><summary>会话说明与日志</summary><p class="section-note">网页停止仅结束本会话的遥操及其专属 PCService，不代替硬件急停。刷新或关闭网页不会停止遥操；建议先结束录制，再停止遥操。</p><h3 class="mt">日志与 LXL 当前配置</h3><pre class="teleop-log" id="teleop-log">${esc(t?.log_tail||'暂无会话日志')}</pre><div class="line-list">${lineList([['LXL 局域网 IP',config.local_ip||'未读到配置']])}${poses}</div></details>`;
 }
 const log=$('#teleop-log');if(log&&log.textContent!==(t?.log_tail||'暂无会话日志'))log.textContent=t?.log_tail||'暂无会话日志';
 $$('[data-teleop="prepare"]').forEach(el=>el.disabled=!app.authenticated||!live||!t?.available||active||t?.can_stop||app.teleopBusy||Boolean(app.lease));
}
async function refreshTeleop(){
 if(app.teleopPollBusy)return;app.teleopPollBusy=true;
 try{const result=await api('/api/teleop/state');app.teleop=result.teleop;app.teleopOnline=true;app.teleopReceived=performance.now();}catch{app.teleopOnline=false;}
 finally{app.teleopPollBusy=false;updatePico();}
}
async function teleopAction(operation,variant){
 if(operation==='prepare'||operation==='confirm')showPicoView('connect');
 if(app.teleopBusy)return;if(!await login())return;
 app.teleopRequestError=null;const t=app.teleop;if(operation!=='stop'&&(!app.teleopOnline||t?.id&&!stateFresh(t,app.teleopReceived,true)))throw Error('遥操状态失效，请等待重新读取');app.teleopBusy=true;updatePico();
 try{const body=operation==='prepare'?{variant}:{session_id:t?.id,...(operation==='confirm'?{nonce:t?.nonce}:{})};const result=await api('/api/teleop/'+operation,body,{timeout:12000});app.teleop=result.teleop;app.teleopOnline=true;app.teleopReceived=performance.now();toast(operation==='prepare'?'正在启动，通过预检后进入遥操':operation==='confirm'?'启动确认已提交':'停止请求已提交');}
 catch(error){app.teleopRequestError=error.message;throw error;}finally{app.teleopBusy=false;await refreshTeleop();}
}
function recordingModeLabel(mode){return {not_started:'尚未启动',starting:'正在启动',idle:'准备就绪',recording:'正在录制',finalizing:'正在整理片段',review:'等待审核保存',error:'录制异常',unavailable:'暂不可用'}[mode]||'等待状态';}
function recordingDuration(value){if(!finite(value))return '—';const seconds=Math.max(0,Math.floor(value));return `${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;}
const activeMediaPlayers=new Set();
function episodeVideos(videos,options={}){
 const available=Array.isArray(videos)?videos.filter(v=>['head','left','right'].includes(v.camera)&&typeof v.url==='string'&&(v.url.startsWith('/api/data/')||v.url.startsWith('/api/recording/'))):[],analytics=typeof options.analytics_url==='string'&&options.analytics_url.startsWith('/api/data/')?options.analytics_url:'';
 if(options.format==='official_lerobot_v3')return `<div class="notice mt">官方 LeRobot v3 · ${esc(options.dataset_episodes)} 个片段 · ${options.training_ready?'已审核导出':'候选数据，待片段审核'}。这是训练文件集；完整操作回放请打开原始采集文件夹。</div>`;
 if(!available.length&&!analytics)return pendingVideoHTML(options);
 return `<section class="recording-video-section synced-review" data-media-viewer data-duration="${finite(options.duration_s)?options.duration_s:0}" data-analytics="${esc(analytics)}"><div class="recording-video-heading"><h3>${options.pending?'本条待保存片段':'视频回看'}</h3><button type="button" class="button small" data-media-focus>专注播放 ↗</button></div><div class="recording-video-grid">${['left','head','right'].map(camera=>{const video=available.find(v=>v.camera===camera),label={head:'头部 · 主视角',left:'左腕相机',right:'右腕相机'}[camera];return `<figure class="recorded-camera" data-review-camera="${camera}"><div class="recorded-frame">${video?`<video preload="metadata" playsinline muted src="${esc(video.url)}" data-camera="${camera}" data-offset="${finite(video.offset_s)?video.offset_s:0}" aria-label="${label}历史片段"></video><span class="video-state" data-video-state="${camera}">加载中</span>`:`<div class="recorded-empty">${icon('camera')}<span>该路暂无视频</span></div>`}</div><figcaption>${label}${video?`<button type="button" class="camera-expand" data-media-zoom="${camera}" aria-label="放大${label}">↗</button>`:''}</figcaption></figure>`;}).join('')}</div><div class="shared-timeline"><div class="playback-controls"><button type="button" class="button primary" data-media-play aria-label="播放三路录制">播放</button><button type="button" class="button small" data-media-skip="-5" aria-label="后退五秒">−5 秒</button><button type="button" class="button small" data-media-skip="5" aria-label="前进五秒">+5 秒</button><output data-media-time>00:00.0 / —</output><input type="number" min="0" step="0.1" data-media-time-input aria-label="视频定位秒数" value="0"><button type="button" class="button small" data-media-note ${app.page==='data'?'':'hidden'}>记时间点</button><label class="speed-control">速度<select data-media-speed aria-label="三路统一播放速度">${[0.25,0.5,1,1.5,2].map(speed=>`<option value="${speed}" ${speed===1?'selected':''}>${speed}×</option>`).join('')}</select></label></div><input type="range" min="0" max="1" value="0" step="0.01" data-media-seek aria-label="三路视频与曲线共用时间轴"><p data-media-status class="media-status">载入视频后使用共用时间轴；点击曲线也可定位。</p></div><section class="joint-overview" data-joint-overview><h3>所有关节 · 跟随概览</h3><p>正在读取关节信息…</p></section><details class="curve-panel" data-curve-details><summary>展开关节曲线与完整统计</summary><div class="curve-toolbar"><div><h3>关节状态与动作</h3><p>与三路画面使用同一片段时间轴</p></div><label>查看通道<select data-curve-channel aria-label="曲线关节通道" disabled><option>等待曲线数据</option></select></label></div><div data-curve-loading class="curve-loading">${analytics?'正在读取曲线…':'此片段尚未提供曲线缓存。'}</div><div data-curve-content hidden><div class="curve-legend"><span><i class="state-line"></i>实测 state <output data-state-value>—</output></span><span><i class="action-line"></i>命令 action <output data-action-value>—</output></span><span data-curve-unit></span></div><canvas data-curve-canvas height="240" role="img" aria-label="选中关节的实测与动作曲线，点击或拖动定位对应视频时间" tabindex="0"></canvas><div class="curve-meta"><span data-curve-sample></span><span>点击 / 拖动曲线定位</span></div><details class="curve-notes"><summary>时间对齐与数据说明</summary><div data-curve-notes></div></details></div></details></section>`;
}
function disposeMediaViewers(scope){if(motionReplay&&(!scope||scope.contains(motionReplay.root))){motionReplay.dispose();motionReplay=null;}for(const player of [...activeMediaPlayers])if(!scope||scope.contains(player.root)){player.dispose();activeMediaPlayers.delete(player);}}
function mountMediaViewers(scope){for(const root of $$('[data-media-viewer]',scope)){if([...activeMediaPlayers].some(player=>player.root===root))continue;activeMediaPlayers.add(new SyncedEpisodePlayer(root));}}
class SyncedEpisodePlayer{
 constructor(root){this.memoryKey='video:'+(app.episodeDetail?.id||root.dataset.analytics||'')+':'+(app.episodeDetail?.source_signature||'');this.root=root;this.videos=$$('video',root);this.time=readContext(this.memoryKey,0);this.speed=1;this.duration=Number(root.dataset.duration)||0;this.playing=false;this.disposed=false;this.channel=0;this.analytics=null;this.phase='paused';this.events=[];
  this.on($('[data-media-play]',root),'click',()=>this.toggle());this.on($('[data-media-seek]',root),'input',event=>this.queueSeek(Number(event.target.value)));this.on($('[data-media-speed]',root),'change',event=>{this.speed=Number(event.target.value);this.syncGroup.setRate(this.speed);});
  this.on($('[data-media-time-input]',root),'change',event=>{const value=Number(event.target.value);this.pause();this.seek(value);});this.on($('[data-media-note]',root),'click',()=>{this.pause();const input=$('#review-notes');if(input){input.value+=(input.value?'\n':'')+`[视频 ${this.time.toFixed(3)}s] `;cacheDataReviewDraft();input.focus();}});
  for(const button of $$('[data-media-skip]',root))this.on(button,'click',()=>this.seek(this.time+Number(button.dataset.mediaSkip)));
  this.on($('[data-curve-channel]',root),'change',event=>{this.channel=Number(event.target.value);this.paint();});
  this.on($('[data-media-focus]',root),'click',()=>{const detail=root.closest('.data-approved-detail')||root;const on=detail.classList.toggle('data-video-focus');$('[data-media-focus]',root).textContent=on?'退出专注 ↙':'专注播放 ↗';});
  for(const b of $$('[data-media-zoom]',root))this.on(b,'click',()=>{const camera=b.closest('figure');camera.requestFullscreen?.().catch(()=>toast('浏览器暂不支持全屏'));});
  this.on($('[data-curve-details]',root),'toggle',()=>this.paintCurve());
  this.on(root,'click',event=>{const b=event.target.closest('[data-joint-channel],[data-metric-time]');if(!b)return;this.channel=Number(b.dataset.jointChannel??b.dataset.metricChannel);$('[data-curve-channel]',root).value=this.channel;$('[data-curve-details]',root).open=true;if(b.dataset.metricTime!==undefined){this.pause();this.seek(Number(b.dataset.metricTime));}this.paintCurve();});
  this.on(document,'keydown',event=>{if(event.defaultPrevented||!root.closest('#episode-detail')||root.closest('[hidden]')||document.querySelector('dialog[open]')||/INPUT|SELECT|TEXTAREA|BUTTON/.test(event.target.tagName))return;if(event.code==='Space'){event.preventDefault();this.toggle();}else if(['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();this.seek(this.time+(event.key==='ArrowLeft'?-2:2));}else if(event.key.toLowerCase()==='m')$('[data-media-note]',root).click();});
  const canvas=$('[data-curve-canvas]',root);let dragging=false;const curveSeek=event=>{const rect=canvas.getBoundingClientRect(),width=rect.width-76;this.queueSeek((event.clientX-rect.left-54)/Math.max(1,width)*this.duration);};this.on(canvas,'pointerdown',event=>{if(event.button!==0)return;dragging=true;canvas.setPointerCapture?.(event.pointerId);curveSeek(event);});this.on(canvas,'pointermove',event=>{if(dragging)curveSeek(event);});for(const event of ['pointerup','pointercancel','lostpointercapture'])this.on(canvas,event,()=>dragging=false);this.on(canvas,'keydown',event=>{if(event.key==='ArrowLeft'||event.key==='ArrowRight'){event.preventDefault();this.seek(this.time+(event.key==='ArrowRight'?1:-1));}});
  for(const video of this.videos){video.muted=true;this.on(video,'loadedmetadata',()=>{if(video.videoWidth&&video.videoHeight)video.parentElement.style.aspectRatio=`${video.videoWidth} / ${video.videoHeight}`;const end=video.duration+Number(video.dataset.offset||0);if(!Number(root.dataset.duration)&&finite(end))this.duration=Math.max(this.duration,end);this.syncGroup?.setDuration(this.duration);this.paint();});this.on(video,'loadeddata',()=>this.paint());}
  this.on(document,'visibilitychange',()=>{if(document.hidden){this.pause();}});
  this.intersection=new IntersectionObserver(es=>{if(!es[0].isIntersecting){this.pause();}});this.intersection.observe(root);
  this.resizeObserver=new ResizeObserver(()=>this.paint());this.resizeObserver.observe(root);
  this.syncGroup=new SynchronizedVideos(this.videos,{time:this.time,duration:this.duration,onChange:state=>{if(this.disposed)return;this.time=state.time;this.playing=state.intent;this.phase=state.phase;const status=$('[data-media-status]',root);status.textContent=state.reason||({buffering:'三路同步缓冲中，等待全部画面就绪',seeking:'正在对齐三路画面',starting:'正在同步开始播放',playing:'三路同步播放 · 与曲线使用同一时间轴',ended:'播放结束',paused:'三路使用同一时间轴'}[state.phase]||'');this.paint();}});
  if(root.dataset.analytics)this.loadAnalytics(root.dataset.analytics);if(!root.dataset.analytics)$('[data-joint-overview]',root).innerHTML='<h3>所有关节 · 跟随概览</h3><p>此录制暂未提供关节统计；缺失值不填零。</p>';this.paint();
 }
 on(target,type,handler){target?.addEventListener(type,handler);this.events.push([target,type,handler]);}
 async loadAnalytics(url){try{const response=await api(url,undefined,{timeout:20000});if(this.disposed)return;const data=response.analytics||response;if(!Array.isArray(data.timestamps_s)||!data.timestamps_s.length||data.timestamps_s.some((t,i,a)=>!finite(t)||(i>0&&t<a[i-1])))throw new Error('曲线时间戳缺失或顺序无效');this.analytics=data;$('[data-joint-overview]',this.root).innerHTML=jointOverviewHTML(data);const dimension=data.channel_names?.length||data.state?.find(Array.isArray)?.length||data.action?.find(Array.isArray)?.length||0;if(!dimension)throw new Error('曲线没有可显示的关节通道');const selector=$('[data-curve-channel]',this.root);selector.innerHTML=Array.from({length:dimension},(_,i)=>{const name=data.channel_names?.[i]||`通道 ${i+1}`,label=name==='gripper_left'?'左夹爪':name==='gripper_right'?'右夹爪':labelJoint(name);return `<option value="${i}">${esc(label)}</option>`;}).join('');selector.disabled=false;const end=finite(data.duration_s)?data.duration_s:data.timestamps_s.at(-1);if(end>0){this.duration=end;this.syncGroup?.setDuration(end);}const deltas=data.timestamps_s.slice(1).map((t,i)=>t-data.timestamps_s[i]).filter(t=>t>0).sort((a,b)=>a-b);this.gapLimit=deltas.length?deltas[Math.floor(deltas.length/2)]*4:Infinity;$('[data-curve-loading]',this.root).hidden=true;$('[data-curve-content]',this.root).hidden=false;const notes=[...(Array.isArray(data.notes)?data.notes:[]),'曲线经过抽样显示；缺失值保留为空，不填零。',data.action?'action 显示采集时实际记录的命令，不代表当前机器人目标。':'此片段未提供 action 命令，仅显示可用的 state 实测。'];$('[data-curve-notes]',this.root).innerHTML=notes.map(note=>`<p>${esc(typeof note==='string'?note:JSON.stringify(note))}</p>`).join('');this.paint();}catch(error){if(!this.disposed){$('[data-curve-loading]',this.root).textContent=`曲线暂不可用：${error.message}`;$('[data-joint-overview]',this.root).innerHTML='<h3>所有关节 · 跟随概览</h3><p>'+esc(error.message)+'</p>';}}}
 toggle(){if(!this.duration||!this.videos.length)return;if(this.playing)this.pause();else this.syncGroup.play();}
 queueSeek(value){this.syncGroup.seek(value,{defer:true});}
 seek(value){this.syncGroup.seek(value);writeContext(this.memoryKey,this.time);}
 stamp(value){if(!finite(value))return '—';const n=Math.max(0,value);return `${String(Math.floor(n/60)).padStart(2,'0')}:${(n%60).toFixed(1).padStart(4,'0')}`;}
 paint(){if(this.disposed)return;const root=this.root,play=$('[data-media-play]',root),seek=$('[data-media-seek]',root);play.textContent=this.playing?'暂停':'播放';play.disabled=!this.duration||!this.videos.length;const timeInput=$('[data-media-time-input]',root);if(timeInput){timeInput.max=this.duration||0;timeInput.disabled=!this.duration;if(document.activeElement!==timeInput)timeInput.value=this.time.toFixed(3);}$('[data-media-note]',root).disabled=!this.duration;play.setAttribute('aria-label',this.playing?'暂停三路录制':'播放三路录制');seek.max=this.duration||1;seek.value=this.time;seek.disabled=!this.duration;$('[data-media-time]',root).textContent=`${this.stamp(this.time)} / ${this.duration?this.stamp(this.duration):'—'}`;for(const video of this.videos){const label=$(`[data-video-state="${video.dataset.camera}"]`,root);if(!label)continue;if(video.error){label.textContent='此路视频不可用';label.hidden=false;continue;}const offset=Number(video.dataset.offset||0);const shown=this.syncGroup?.frameTime(video);label.hidden=video.readyState>=2&&!video.seeking&&shown!==null&&shown!==undefined&&Math.abs(shown+offset-this.time)<.1&&this.time>=offset&&(!finite(video.duration)||this.time<offset+video.duration);if(!label.hidden)label.textContent=this.time<offset?'尚未到该路起点':finite(video.duration)&&this.time>=offset+video.duration?'该路已结束':'加载中';}this.paintCurve();}
 paintCurve(){const data=this.analytics;if(!data||!$('[data-curve-details]',this.root)?.open)return;const canvas=$('[data-curve-canvas]',this.root),rect=canvas.getBoundingClientRect();if(rect.width<20)return;const width=rect.width,height=240,dpr=Math.min(window.devicePixelRatio||1,2);if(canvas.width!==Math.round(width*dpr)||canvas.height!==height*dpr){canvas.width=Math.round(width*dpr);canvas.height=height*dpr;}const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,width,height);ctx.fillStyle='#fcfaf5';ctx.fillRect(0,0,width,height);const times=data.timestamps_s,channel=this.channel,series=[{rows:data.state,color:'#3d5d70'},{rows:data.action,color:'#bd7858'}],values=series.flatMap(s=>Array.isArray(s.rows)?s.rows.map(row=>row?.[channel]).filter(finite):[]),left=54,right=width-22,top=18,bottom=height-33;if(!values.length){ctx.fillStyle='#84796a';ctx.font='14px sans-serif';ctx.fillText('此通道没有有效值',left,80);return;}let min=Math.min(...values),max=Math.max(...values),pad=Math.max((max-min)*.12,.5);min-=pad;max+=pad;const x=time=>left+time/Math.max(this.duration,.001)*(right-left),y=value=>bottom-(value-min)/(max-min)*(bottom-top);ctx.font='11px sans-serif';ctx.lineWidth=1;for(let i=0;i<=4;i++){const yy=top+(bottom-top)*i/4;ctx.strokeStyle='#e8e1d5';ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(right,yy);ctx.stroke();ctx.fillStyle='#958a79';ctx.textAlign='right';ctx.fillText((max-(max-min)*i/4).toFixed(1),left-9,yy+4);}for(let i=0;i<=4;i++){const xx=left+(right-left)*i/4;ctx.fillStyle='#958a79';ctx.textAlign=i===0?'left':i===4?'right':'center';ctx.fillText(this.stamp(this.duration*i/4),xx,height-10);}ctx.save();ctx.beginPath();ctx.rect(left,top,right-left,bottom-top);ctx.clip();for(const seriesEntry of series){if(!Array.isArray(seriesEntry.rows))continue;ctx.strokeStyle=seriesEntry.color;ctx.lineWidth=1.7;ctx.beginPath();let connected=false,lastTime=null;for(let i=0;i<times.length;i++){const value=seriesEntry.rows[i]?.[channel];if(!finite(value)){connected=false;continue;}const xx=x(times[i]),yy=y(value);if(connected&&lastTime!==null&&times[i]-lastTime<=this.gapLimit)ctx.lineTo(xx,yy);else ctx.moveTo(xx,yy);connected=true;lastTime=times[i];}ctx.stroke();}const cursor=x(this.time);ctx.strokeStyle='#474038';ctx.lineWidth=1;ctx.setLineDash([4,3]);ctx.beginPath();ctx.moveTo(cursor,top);ctx.lineTo(cursor,bottom);ctx.stroke();ctx.setLineDash([]);let lo=0,hi=times.length-1;while(lo<hi){const mid=Math.ceil((lo+hi)/2);if(times[mid]<=this.time)lo=mid;else hi=mid-1;}const unit=data.units?.[channel]||'',unitLabel=unit==='deg'?'°':['percent','%'].includes(unit)?'%':unit;for(const [index,seriesEntry] of series.entries()){const value=seriesEntry.rows?.[lo]?.[channel];$(`[data-${index===0?'state':'action'}-value]`,this.root).textContent=finite(value)?`${fmt(value,2)}${unitLabel}`:'—';if(finite(value)){ctx.fillStyle=seriesEntry.color;ctx.beginPath();ctx.arc(x(times[lo]),y(value),3,0,Math.PI*2);ctx.fill();}}ctx.restore();$('[data-curve-unit]',this.root).textContent=unitLabel?`单位 ${unitLabel}`:'';$('[data-curve-sample]',this.root).textContent=`${times.length.toLocaleString()} 个显示采样点${finite(data.source_frame_count)?` / ${data.source_frame_count.toLocaleString()} 帧`:''}`;}
 pause(){this.syncGroup?.pause();writeContext(this.memoryKey,this.time);this.paint();}
 dispose(){writeContext(this.memoryKey,this.time);this.disposed=true;this.syncGroup?.dispose();this.resizeObserver.disconnect();this.intersection.disconnect();this.videos.forEach(video=>{video.pause();video.removeAttribute('src');video.load();});this.events.forEach(([target,type,handler])=>target?.removeEventListener(type,handler));}
}

function renderRecording(){
 const r=app.recording,indicator=$('#recording-indicator'),mode=r?.mode,ongoing=['starting','recording','finalizing','review','error'].includes(mode);
 if(indicator){indicator.hidden=!ongoing;indicator.classList.toggle('is-recording',mode==='recording');indicator.classList.toggle('is-review',mode==='review');indicator.textContent=mode==='recording'?`● 录制中 ${recordingDuration(r.elapsed_s)}`:mode==='review'?'● 录制待保存':`● ${recordingModeLabel(mode)}`;if(!app.recordingOnline&&ongoing)indicator.textContent='● 录制状态失联';}
 const summary=$('#pico-record-summary');if(summary)summary.textContent=app.recordingOnline?recordingModeLabel(mode):'状态失联';
 const holder=$('#recorder-card');if(!holder)return;
 if(!r){holder.innerHTML=`${cardHeader('独立录制','原 LXL 采集保持现状')}${empty('录制服务未连接','等待独立录制器状态。','data')}`;return;}
 const signature=JSON.stringify({mode:r.mode,available:r.available,ready:r.ready,reasons:r.ready_reasons,output_root:r.output_root,task:r.task,frames:r.frames,elapsed_s:r.mode==='recording'?r.elapsed_s:null,review:r.review,last_saved:r.last_saved,last_error:r.last_error,online:app.recordingOnline,authenticated:app.authenticated,busy:app.recordingBusy,requestError:app.recordingRequestError});if(holder.dataset.viewSignature===signature)return;holder.dataset.viewSignature=signature;
 const reasons=Array.isArray(r.ready_reasons)?r.ready_reasons:[],canStart=app.recordingOnline&&r.available&&(mode==='not_started'||(mode==='idle'&&r.ready))&&!app.recordingBusy,canStop=app.recordingOnline&&mode==='recording'&&!app.recordingBusy,canSave=app.recordingOnline&&mode==='review'&&!app.recordingBusy;
 disposeMediaViewers(holder);
 const stateType=mode==='recording'?'bad':mode==='error'||mode==='unavailable'?'warn':mode==='review'?'warn':r.ready?'good':'neutral';
 holder.innerHTML=`${cardHeader('独立录制',`双臂与夹爪 · 16D / ${finite(r.fps)?r.fps:'—'} FPS`,pill(app.recordingOnline?(mode==='idle'&&!r.ready?'等待采集条件':recordingModeLabel(mode)):'状态失联',stateType))}<div class="recorder-layout"><div class="recorder-info"><label class="field-label">任务名称</label><div class="read-only-field">${esc(r.task||'F1 upper-body bimanual teleoperation')}</div><details class="recorder-location"><summary>保存位置</summary><div class="read-only-field recorder-path">${esc(r.output_root||'等待目录配置')}</div></details><p class="section-note">开始采集不启动 Pico 或机器人动作。切换页面不会结束录制。</p></div><div class="recorder-session"><div class="recording-metrics"><div><strong>${recordingDuration(mode==='review'?r.review?.duration_s:r.elapsed_s)}</strong><span>片段时长</span></div><div><strong>${finite(mode==='review'?r.review?.frames:r.frames)?(mode==='review'?r.review.frames:r.frames).toLocaleString():'—'}</strong><span>已采集帧</span></div></div><div class="recorder-actions">${!app.authenticated?'<button class="button" data-click="login">启用录制</button>':''}${['not_started','idle','unavailable'].includes(mode)?`<button class="button primary" data-recording="start" ${!canStart||!app.authenticated?'disabled':''}>${mode==='not_started'?'启动独立录制服务':'开始录制'} ${icon('plus')}</button>`:''}${mode==='recording'?`<button class="button recorder-stop" data-recording="stop" ${!canStop||!app.authenticated?'disabled':''}>结束录制</button>`:''}${mode==='review'?`<button class="button primary" data-recording="save" ${!canSave||!app.authenticated?'disabled':''}>保存这条录制 ${icon('download')}</button>`:''}${['starting','finalizing'].includes(mode)?`<div class="recorder-processing"><span class="recording-spinner"></span>${mode==='starting'?'正在准备采集…':'正在整理片段，请稍候…'}</div>`:''}${mode==='error'?'<p class="review-note">本条暂不接受新操作。请先查看下方恢复信息。</p>':''}</div></div></div>${!app.recordingOnline?'<div class="notice warn mt">未能获取录制器最新状态。不要重复启动；连接恢复后将重新显示真实状态。</div>':''}${reasons.length&&mode!=='recording'?`<div class="recording-reasons"><strong>${mode==='review'?'片段检查':'开始前检查'}</strong><ul>${reasons.map(reason=>`<li>${esc(typeof reason==='string'?reason:reason.message||reason.reason||JSON.stringify(reason))}</li>`).join('')}</ul></div>`:''}${app.recordingRequestError?`<div class="notice warn mt">${esc(app.recordingRequestError)} <button class="button small" data-click="recording-refresh">重新读取状态</button></div>`:''}${r.last_error?`<div class="notice warn mt">${esc(r.last_error)}</div>`:''}${r.last_saved&&mode!=='review'?`<div class="recording-last-saved"><strong>${r.last_saved.kind==='recovery'?'上次保存：恢复数据':'上次保存：正式片段'}</strong><span>${esc(r.last_saved.relative_path||r.last_saved.path||r.last_saved.id||'已保存在独立目录')}</span>${r.last_saved.data_episode_id&&r.last_saved.kind!=='recovery'?`<a class="button small" href="#data?episode=${esc(r.last_saved.data_episode_id)}">查看这条</a>`:''}${r.last_saved.kind==='recovery'?'<p>本条未通过完整性检查，已保留原始观测用于排查，不进入正式训练数据。</p>':''}</div>`:''}${r.review&&mode==='review'?`<div class="recording-review-summary"><strong>${r.review.raw_save_allowed?'原始采集已结束 · 待保存':r.review.coverage_fault?'采集覆盖检查发现问题':r.review.training_ready?'采集检查通过':'完整性未通过，将保存为恢复数据'}</strong><p>${r.review.raw_save_allowed?'保存后可在文件夹中检查原始三图；转换和训练审核单独进行。':r.review.coverage_fault?esc(typeof r.review.coverage_fault==='string'?r.review.coverage_fault:JSON.stringify(r.review.coverage_fault)):(r.review.training_ready?'保存后可在「数据」页查看已生成的视频，并补充审核结论。':'保存会保留当前观测供排查，不进入正式 LeRobot 训练片段。')}</p><span>训练可用性仍以数据合同验收为准。</span></div>${r.review.raw_save_allowed?'':episodeVideos(r.review.videos,{...r.review,pending:true,duration_s:r.review.duration_s})}`:''}`;
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
 app.recordingRequestError=null;app.recordingBusy=true;renderRecording();
 try{const result=await api(`/api/recording/${action}`,{},{timeout:120000});if(result.recording){app.recording=result.recording;app.recordingOnline=true;}await refreshRecording();
  if(action==='start')toast(app.recording?.mode==='idle'?'独立录制服务已启动；条件就绪后点击开始录制':app.recording?.mode==='recording'?'独立上肢录制已开始':'启动请求已提交，请查看录制器状态');
  if(action==='stop')toast(app.recording?.mode==='review'?'本条已结束，请检查片段后明确保存':'结束请求已处理，请查看录制器状态');
  if(action==='save'){toast(app.recording?.last_saved?.kind==='recovery'?'本条已保存为恢复数据，未进入正式训练片段':'录制已保存');await refreshData();}
 }catch(error){app.recordingRequestError=error.message;toast(error.message,true);await refreshRecording();}
 finally{app.recordingBusy=false;renderRecording();}
}
function formatBytes(bytes){if(!finite(bytes))return '—';return bytes>=1e9?`${fmt(bytes/1e9,2)} GB`:bytes>=1e6?`${fmt(bytes/1e6,1)} MB`:`${fmt(bytes/1e3,1)} KB`;}
function formatDate(value,timeZone){const time=recordTime(value);return Number.isFinite(time)?new Date(time).toLocaleString('zh-CN',{hour12:false,...(timeZone?{timeZone}:{})}):'—';}
function reviewLabel(value){return {pending:'待审核',approved:'已通过',rejected:'需排查'}[value]||'待审核';}
function savedReviewBadge(e){const status=e.review?.stale?'pending':e.review?.status;return `<span data-saved-review class="pill ${status==='approved'?'good':status==='rejected'?'warn':'neutral'}">${esc(e.recording_status==='committed'?`已提交 · ${reviewLabel(status)}`:'恢复数据 · 未完整提交')}</span>`;}
function renderDataStorage(){
 const holder=$('#data-storage');if(!holder)return;
 const stale=!app.apiOnline||Date.now()-app.lastReceived>10000||!!app.dataReadFailed;
 const html=storageHTML(app.data,app.state?.system_health?.disk,{stale});
 if(holder.innerHTML!==html)holder.innerHTML=html;const summary=$('#data-storage-summary');if(summary)summary.textContent=finite(app.state?.system_health?.disk?.free_bytes)?`${stale?'上次可用':'可用'} ${formatBytes(app.state.system_health.disk.free_bytes)}`:'存储空间 · 未读取';
}
function updateData(){
 if(app.page!=='data')return;renderDataStorage();const d=app.state?.data||{},disk=app.state?.system_health?.disk||{},recording=app.data?.recording||{};
 const status={observed_active:'检测到录制相关进程',no_process_observed:'未检测到录制进程',unknown:'状态未确认'}[recording.state]||d.recorder_status||'正在读取';
 $('#data-status').innerHTML=`<div class="line-list">${lineList([['录制状态',status],['本地目录',app.data?.root||d.local_path||d.path||'未读取'],['本机可用磁盘',finite(disk.free_bytes)?`${fmt(disk.free_bytes/(1024**3),1)} GiB`:'未读取'],['预计剩余容量',finite(disk.episodes_at_1GB_reserve_100GB)?`约 ${Math.floor(disk.episodes_at_1GB_reserve_100GB)} 条（每条 1 GB，保留 100 GB）`:'按实际片段大小确定'],['自动上传',d.transfer_status||'保持原配置；未确认开启'],['中央平台',d.central_status||'未配置']])}</div>${recording.message?`<p class="section-note">${esc(recording.message)}</p>`:''}`;
}
async function refreshData(){
 if(dataOrganization.engaged)return;const organizationEpoch=dataOrganization.epoch;
 if(app.dataBusy)return;app.dataBusy=true;
 try{const catalog=await api('/api/data');if(organizationEpoch!==dataOrganization.epoch)return;app.data=catalog;app.dataReadFailed=false;if(app.page==='data'){const selected=app.data.episodes?.find(e=>e.id===app.selectedEpisode);if(app.episodeDetail){if(selected){const changed=selected.source_signature!==app.episodeDetail.source_signature;app.episodeDetail={...app.episodeDetail,...selected};if(changed)app.dataDetailIssue='来源已更新，请重新读取后编辑。';else if(!app.data.error&&!app.data.errors?.length&&/目录/.test(app.dataDetailIssue||''))app.dataDetailIssue=null;}else app.dataDetailIssue=app.data.error||app.data.errors?.length?'目录暂未完整读取，请刷新重试。':'录制已不可用；草稿已保留。';}renderDataCatalog();updateData();}}
 catch(error){if(organizationEpoch!==dataOrganization.epoch)return;app.dataReadFailed=true;app.dataDetailIssue='目录暂时读取失败，请刷新重试。';renderDataStorage();if(app.page==='data'){updateReviewStatus();$('#data-folders').innerHTML=empty('暂时无法读取数据',error.message,'data');$('#episode-count').textContent='读取失败';}}
 finally{app.dataBusy=false;}
}
let dataSelecting=false,dataDeleting=false;
const savedDataFilters=readContext('data-filters',{});
const dataFilters={query:'',page:1,sourceMode:'',browseMode:'time',assessmentFilter:'',...(savedDataFilters.version===3?savedDataFilters:{}),version:3,scope:'folder',reviewStatus:'',dateFrom:'',dateTo:''};
let dataReturn=readContext('data-return',{}),dataQueue=readContext('data-queue',[]),dataNavigation=0;
app.dataFolder=dataReturn.folder||null;
const dataReviewDrafts=new Map(readContext('review-drafts',[])),dataReviewSaving=new Set();
const persistReviewDrafts=()=>writeContext('review-drafts',[...dataReviewDrafts]);
const dataCovers=new DataCovers();
const dataNotesEditor=new DataNotesEditor({api,esc,formatDate,drafts:dataReviewDrafts,persist:persistReviewDrafts,
 getEpisode:id=>app.data?.episodes?.find(e=>e.id===id),
 onSaved:(id,source,review)=>{
  const row=app.data?.episodes?.find(e=>e.id===id);if(row?.source_signature===source)row.review=review;
  if(app.episodeDetail?.id===id&&app.episodeDetail.source_signature===source){app.episodeDetail.review=review;app.reviewDirty=dataReviewDrafts.has(id);}
  if(app.page==='data')renderDataCatalog();toast('备注已保存');
 }});
const detailKey=e=>JSON.stringify([e.id,e.source_signature,e.videos,e.analytics_url,e.preview_status]);
let dataDetailGeneration=0;
const dataSelection=new Map();
const dataOrganization=new DataOrganizationUI({get:()=>({page:app.page,data:app.data,folder:app.dataFolder,browseMode:dataFilters.browseMode,dataReadFailed:app.dataReadFailed,deleting:dataDeleting}),api,login,render:renderDataCatalog,refresh:refreshData,selection:()=>[...dataSelection.values()],esc});
let dataDeleteResult=null,dataDeleteUncertain=false;
const deletable=e=>['lxl','hil_demonstration','hil_policy_hil','pico_capture'].includes(e.source);
function resetDataSelection(){dataSelecting=false;dataSelection.clear();dataDeleteUncertain=false;}
function toggleDataSelection(id){
 if(dataDeleting)return;
 const e=app.data?.episodes?.find(e=>e.id===id);if(!e||!deletable(e))return;
 if(dataSelection.has(id))dataSelection.delete(id);else {if(dataSelection.size>=100){toast('每次最多删除 100 条');return;}dataSelection.set(id,{id,source_signature:e.source_signature,name:e.name,size_bytes:e.size_bytes});}
 renderDataCatalog();
}
async function deleteSelectedData(){
 if(dataDeleting||dataOrganization.engaged||dataDeleteUncertain||!dataSelection.size)return;
 const selected=[...dataSelection.values()],invalid=selected.some(row=>app.data?.episodes?.find(e=>e.id===row.id)?.source_signature!==row.source_signature);
 if(invalid){toast('所选来源已变化或不可用，请重新选择',true);return;}
 dataDeleting=true;dataDeleteResult=null;renderDataCatalog();
 try{
  const result=await api('/api/data/delete',{confirm:true,episodes:selected.map(({id,source_signature})=>({id,source_signature}))},{timeout:60000});
  const deleted=result.results.filter(r=>r.state==='deleted');for(const row of deleted){dataReviewDrafts.delete(row.id);dataSelection.delete(row.id);}persistReviewDrafts();
  dataDeleteResult={text:`已删除 ${deleted.length} 条`,transaction:result.transaction,results:result.results.filter(r=>r.state!=='deleted')};
  if(result.complete)resetDataSelection();else dataDeleteUncertain=true;
 }catch(error){dataDeleteUncertain=true;dataDeleteResult={text:`结果待核对：${error.message}`,results:[]};}
 finally{dataDeleting=false;await Promise.allSettled([refreshData(),refreshState(),refreshSystem()]);renderDataCatalog();}
}
function dataLibraryPage(){return folderPage(libraryView(app.data,app.dataFolder||''),dataFilters);}
function showDataWorkspace(){
 const detail=!!app.selectedEpisode;$('#data-library-view').hidden=detail;$('#episode-detail').hidden=!detail;
 if($('#data-header-actions'))$('#data-header-actions').hidden=detail;
}
function cacheDataReviewDraft(){
 const id=app.selectedEpisode;if(!id||!app.episodeDetail||!$('#review-status')||!$('#review-notes'))return;
 dataReviewDrafts.set(id,{status:$('#review-status').value,notes:$('#review-notes').value,source_signature:dataReviewDrafts.get(id)?.source_signature||app.episodeDetail.source_signature,review_revision:dataReviewDrafts.get(id)?.review_revision??app.episodeDetail.review?.revision,version:(dataReviewDrafts.get(id)?.version||0)+1});persistReviewDrafts();app.reviewDirty=true;const notes=$('[data-timestamp-notes]');if(notes)notes.innerHTML=timestampNotesHTML($('#review-notes').value);$$('[data-review-choice]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.reviewChoice===$('#review-status').value)));$('#review-saved').textContent='未保存';const discard=$('[data-click="data-review-discard"]');if(discard)discard.hidden=false;
}
async function discardDataReviewDraft(){const id=app.selectedEpisode;if(!id||dataReviewSaving.has(id))return;dataReviewDrafts.delete(id);persistReviewDrafts();app.reviewDirty=false;app.dataDetailIssue=null;await selectEpisode(id);}
function returnDataLibrary(){
 dataDetailGeneration++;dataNavigation++;
 const holder=$('#episode-detail');disposeMediaViewers(holder);app.selectedEpisode=null;app.episodeDetail=null;app.reviewDirty=false;app.dataDetailIssue=null;holder.innerHTML='';delete holder.dataset.episodeSignature;
 history.pushState(null,'','#data');renderDataCatalog();restoreDataPosition();
}
function restoreDataPosition(){requestAnimationFrame(()=>{window.scrollTo(0,dataReturn.scroll||0);$(`[data-episode="${dataReturn.id}"]`)?.focus({preventScroll:true});});}
async function openDataEpisode(id){
 if(!app.selectedEpisode){dataReturn={folder:app.dataFolder,id,scroll:window.scrollY};writeContext('data-return',dataReturn);dataQueue=dataLibraryPage().matching.map(e=>e.id);if(!dataQueue.includes(id))dataQueue=[id];writeContext('data-queue',dataQueue);}
 history.pushState({dataEpisode:true},'','#data?episode='+encodeURIComponent(id));await selectEpisode(id);
}
async function adjacentEpisode(direction){
 if(app.dataReadFailed||app.data?.error||app.data?.errors?.length){toast('目录暂未完整读取，请刷新后继续审核',true);return;}
 const available=new Set((app.data?.episodes||[]).map(e=>e.id));
 dataQueue=dataQueue.filter(id=>available.has(id)||id===app.selectedEpisode);writeContext('data-queue',dataQueue);
 const index=dataQueue.indexOf(app.selectedEpisode);
 for(let i=index+direction;i>=0&&i<dataQueue.length;i+=direction){if(available.has(dataQueue[i]))return openDataEpisode(dataQueue[i]);}
 toast(direction>0?'本轮处理完成，已到最后一条':'已到第一条');
}
function reviewNavigation(){const i=dataQueue.indexOf(app.selectedEpisode);return `<div class="data-review-nav"><button class="button small" data-click="data-back">← 返回${dataFilters.browseMode==='time'?'全部录制':'文件夹'}</button><div><button class="button small" data-click="data-previous" ${i<=0?'disabled':''}>上一条</button><span>${i>=0?`${i+1} / ${dataQueue.length}`:'单条录制'}</span><button class="button small" data-click="data-next" ${i<0||i>=dataQueue.length-1?'disabled':''}>下一条</button></div></div>`;}
function updateReviewStatus(){
 const e=app.episodeDetail,form=$('#review-form');if(!e||!form)return;
 const draft=dataReviewDrafts.get(e.id),sourceChanged=!!draft&&draft.source_signature!==e.source_signature,conflict=!!draft&&draft.review_revision!==e.review?.revision;
 const notice=$('#data-detail-notice'),blocked=sourceChanged||!!app.dataDetailIssue;
 notice.hidden=!(blocked||conflict);notice.innerHTML=blocked?`${esc(app.dataDetailIssue||'来源已更新，请重新读取后编辑。')} <button class="button small" data-click="data-review-discard">重新读取</button>`:conflict?`备注或审核已更新：${esc(reviewLabel(e.review?.status))} · ${esc(e.review?.reviewer||'另一操作者')} <button class="button small" data-click="data-review-reconcile">核对最新内容</button>`:'';
 if(!draft){$('#review-status').value=e.review?.status||'pending';$('#review-notes').value=e.review?.notes||'';}$$('[data-review-choice]',$('#episode-detail')).forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.reviewChoice===$('#review-status').value)));const notes=$('[data-timestamp-notes]',form);if(notes)notes.innerHTML=timestampNotesHTML($('#review-notes').value);
 const badge=$('[data-saved-review]');if(badge){badge.outerHTML=savedReviewBadge(e);if(app.dataDetailIssue&&$('#episode-detail [data-saved-review]'))$('#episode-detail [data-saved-review]').textContent='上次审核 · '+reviewLabel(e.review?.status);}
 $$('[data-review-save]',$('#episode-detail')).forEach(b=>b.disabled=blocked||dataReviewSaving.has(e.id));
}
async function reconcileReview(){
 const e=app.episodeDetail,draft=dataReviewDrafts.get(e?.id);if(!draft)return;
 if(await confirmDataDialog('审核已被更新',`<p>最新结论：${esc(reviewLabel(e.review?.status))} · ${esc(e.review?.reviewer||'')}</p><pre>${esc(e.review?.notes||'无备注')}</pre><p>保留当前草稿，确认后可重新保存。</p>`,'保留草稿并重新确认')){draft.review_revision=e.review?.revision;persistReviewDrafts();updateReviewStatus();}
}
function confirmDataDialog(title,body,label='确认删除'){
 return new Promise(resolve=>{const origin=document.activeElement,d=document.createElement('dialog');d.className='execute-dialog data-confirm';d.innerHTML=`<h2>${esc(title)}</h2><div class="data-confirm-body">${body}</div><div class="dialog-actions"><button class="button" data-cancel>取消</button><button class="button primary" data-confirm>${label}</button></div>`;document.body.append(d);let approved=false;d.querySelector('[data-cancel]').onclick=()=>d.close();d.querySelector('[data-confirm]').onclick=()=>{approved=true;d.close();};d.onclose=()=>{d.remove();origin?.focus({preventScroll:true});resolve(approved);};d.showModal();d.querySelector('[data-cancel]').focus();});
}
function assessmentTone(a){return a?.split==='train'?'good':a?.split==='validation'?'info':'neutral';}
function assessmentBadges(e){const a=rltAssessment(e);return a?`<div class="rlt-status-line">${pill(a.outcome_label||'结果待核实',a.outcome==='failure'?'warn':'neutral')}${pill(a.confirmation_label||'待审核','neutral')}${pill(a.usage_label||'待审核',assessmentTone(a))}</div>`:'';}
function rltSummaryHTML(view){
 const records=view.scoped.filter(e=>e.capture_mode==='rlt');if(!records.length||!view.breadcrumbs.some(c=>c.path==='HIL/RLT'))return '';
 const counts={train:0,validation:0,excluded:0,pending:0};let manual=0;for(const e of records){const a=rltAssessment(e);counts[a.split]++;if(a.confirmation_kind==='manual_confirmation')manual++;}
 return `<div class="rlt-summary" data-rlt-summary><strong>共 ${records.length} 条录制</strong><span>训练局部 <b>${counts.train}</b> 条</span><span>开发验证 <b>${counts.validation}</b> 条</span><span>不入局部训练 <b>${counts.excluded}</b> 条</span><span>待审核 <b>${counts.pending}</b> 条</span></div><div class="rlt-filter-bar" aria-label="局部审核筛选">${[['','全部'],['pending',`待审核 ${counts.pending}`],['manual_confirmation',`人工补确认 ${manual}`]].map(([key,label])=>`<button class="button small" data-assessment-filter="${key}" aria-pressed="${dataFilters.assessmentFilter===key}">${label}</button>`).join('')}</div>`;
}
function folderSummaryHTML(view){
 if(dataFilters.browseMode==='time'||!view.current||view.breadcrumbs.some(c=>c.path==='HIL/RLT')&&!view.folders.length)return '';
 const total=view.scoped.length,children=total-view.leaves.length;
 return `<div class="data-folder-summary" data-folder-summary><strong>共 ${total} 条录制</strong><span>本层 ${view.leaves.length} 条</span><span>子文件夹 ${children} 条</span></div>`;
}
function rltRangeHTML(e){
 const a=rltAssessment(e);if(!a)return '';
 const start=a.local_start_s,terminal=a.terminal_s,end=a.original_end_s;
 if(![start,terminal,end].some(finite))return '';
 return `<section class="rlt-range"><h3>局部审核范围</h3><div class="rlt-range-points">${finite(start)?`<div><small>进入局部</small><strong>${fmt(start,2)} s</strong></div>`:''}${finite(terminal)?`<div><small>审核终点</small><strong>${fmt(terminal,2)} s</strong></div>`:''}${finite(end)?`<div><small>原始局部结束</small><strong>${fmt(end,2)} s</strong></div>`:''}</div><p>时间相对本条录制开始。${finite(a.retained_seconds)?`有效转移累计 ${fmt(a.retained_seconds,2)} 秒；`:' '}${esc(a.excluded_summary||'仅使用审核范围，未执行与质量不合格的区间排除。')}</p></section>`;
}
function rltDetailHTML(e){
 const a=rltAssessment(e);if(!a)return '';
 const rows=[['原始终止',a.original_status],['有效数据',finite(a.transition_count)?`${a.transition_count} 转移${finite(a.retained_seconds)?` / ${fmt(a.retained_seconds,2)} s`:''}`:null],['终点观测',a.terminal_observation_id!==undefined?String(a.terminal_observation_id):null],['排除部分',a.excluded_summary]];
 return `<section class="rlt-assessment-panel"><h3>局部数据审核</h3>${assessmentBadges(e)}<dl>${rows.filter(([,value])=>value!==undefined&&value!==null&&value!=='').map(([key,value])=>`<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join('')}</dl><p class="rlt-assessment-boundary">局部审核与下方人工备注分别保存；采集来源、整条保存和任务成功均不等于整条可训练。</p>${a.notes_summary||a.source_label||a.reviewed_at||a.quality?`<details><summary>审核依据与来源</summary>${a.notes_summary?`<p>${esc(a.notes_summary)}</p>`:''}${a.source_label?`<p>${esc(a.source_label)}</p>`:''}${a.reviewed_at?`<p>审核时间 ${esc(formatDate(a.reviewed_at,e.display_timezone))}</p>`:''}${a.quality?`<p>${esc(({reviewed:'已核对源绑定审核快照',pending_review:'待审核',missing_control:'缺少实际控制记录',recording_incomplete:'录制未完整提交',no_local_segment:'未进入局部RLT',source_changed:'来源已变化，待重新审核',review_invalid:'审核证据无效',unreadable:'暂时无法读取审核证据'})[a.quality]||'审核状态待核对')}</p>`:''}</details>`:''}</section>`;
}
function refreshRLTDetail(e){for(const [selector,html] of [['[data-rlt-assessment]',rltDetailHTML(e)],['[data-rlt-range]',rltRangeHTML(e)]]){const node=$(selector,$('#episode-detail'));if(node&&node._rltMarkup!==html){const open=node.querySelector('details')?.open;node.innerHTML=html;node._rltMarkup=html;if(open&&node.querySelector('details'))node.querySelector('details').open=true;}}}
const recordingSourceLabels={lxl:'LXL',rollout:'真机Rollout',demonstration:'人工示教',dagger:'DAgger',rlt:'RLT',unknown:'待确认来源',shadow:'Shadow测试'};
function recordingDate(e){
 const value=recordTime(e.started_at||e.modified_at);if(!Number.isFinite(value))return '时间未记录';
 const date=new Date(value),options={timeZone:e.display_timezone||'Asia/Shanghai',hour12:false,month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'};
 const currentYear=new Date().toLocaleString('en',{timeZone:options.timeZone,year:'numeric'});
 if(date.toLocaleString('en',{timeZone:options.timeZone,year:'numeric'})!==currentYear)options.year='numeric';
 return date.toLocaleString('zh-CN',options);
}
function recordingListHeading(view,page){
 const time=dataFilters.browseMode==='time',filtered=dataFilters.query||dataFilters.sourceMode||dataFilters.assessmentFilter;
 const foldersOnly=!time&&page.folders.length&&!page.leaves.length;
 const title=time?'全部录制':foldersOnly?'文件夹':'录制数据';
 const count=foldersOnly?`${view.folders.length} 个 · ${view.scoped.length} 条录制`:`${filtered?'匹配 ':''}${page.matching.length} 条`;
 const sources=[['','全部来源'],...Object.entries(recordingSourceLabels).filter(([key])=>['lxl','rollout','demonstration','dagger','rlt'].includes(key)||dataFilters.sourceMode===key||view.records.some(e=>captureMode(e)===key))];
 return `<div class="recording-list-heading"><div class="recording-list-title"><h3>${title}</h3><span>${count}</span></div><div class="recording-list-controls"><select data-data-filter="sourceMode" aria-label="录制来源">${sources.map(([value,label])=>`<option value="${value}" ${dataFilters.sourceMode===value?'selected':''}>${label}</option>`).join('')}</select><span class="recording-sort">最新在前 ↓</span></div></div>`;
}
function recordingTableHTML(rows,view,page){
 return `<div class="recording-table-wrap"><table class="data-recording-table"><colgroup>${['time','source','outcome','usage','notes','action'].map(c=>`<col class="recording-col-${c}">`).join('')}</colgroup><thead><tr><th scope="col">录制时间</th><th scope="col">来源</th><th scope="col">任务结果</th><th scope="col">数据用途</th><th scope="col">备注</th><th scope="col">回看</th></tr></thead><tbody>${rows.map(e=>{
  const selecting=dataSelecting&&deletable(e),selected=dataSelection.has(e.id),[video,videoTone]=previewState(e),note=e.review?.stale?'':e.review?.notes||'',a=rltAssessment(e),mode=captureMode(e);
  // Editable notes and generic approval never manufacture a task success or training eligibility.
  const outcome=a?({assisted_success:'辅助成功',autonomous_success:'自主成功',failure:'局部失败',interrupted:'局部中断'}[a.outcome]||a.outcome_label||'待核实'):'未标注';
  const outcomeTone=a?(['assisted_success','autonomous_success'].includes(a.outcome)?'good':a.outcome==='failure'?'bad':a.outcome==='interrupted'?'warn':'neutral'):'neutral';
  const confirmation=a?({manual_confirmation:'人工补确认',live_success:'现场 ↑ 标记',reviewed_failure:'审核确认',legacy_save:'旧协议仅保存'}[a.confirmation_kind]||(a.quality==='recording_incomplete'?'尚未完整封存':'结果待审核')):(e.review?.stale?'审核来源已变化':e.review?.status==='approved'?'人工审核通过':e.review?.status==='rejected'?'人工审核需排查':'结果待审核');
  const usage=a?({train:'训练可用',validation:'开发验证',excluded:'不入训练',pending:'待审核'}[a.split]||'待审核'):'用途未审核';
  const usageTone=a?(a.split==='pending'?'warn':assessmentTone(a)):'neutral';
  const meta=[finite(e.duration_s)?fmt(e.duration_s,1)+' 秒':'时长未记录',...(!e.videos?.length?[video]:[])].join(' · ');
  const shortNote=note.replace(/^【[0-9-]+ 数据核对】\s*/,''),sourceLabel=e.capture_label||recordingSourceLabels[mode]||'待确认来源';
  return `<tr data-recording-id="${esc(e.id)}" ${e.source!=='unified_dataset'?`draggable="true" data-drag-episode="${esc(e.id)}"`:''} class="${selected?'delete-selected':''}"><td><div class="recording-row-name">${selecting?`<button class="recording-row-check" data-select-episode="${esc(e.id)}" role="checkbox" aria-checked="${selected}" aria-label="选择 ${esc(e.name)}">${selected?'✓':''}</button>`:`<span class="recording-file-icon" data-cover="${esc(e.id)}" aria-hidden="true">${icon('camera')}</span>`}<button class="recording-row-open" data-episode="${esc(e.id)}" title="${esc(e.name)} · ${esc(formatDate(e.started_at||e.modified_at,e.display_timezone||'Asia/Shanghai'))}" ${dataDeleting?'disabled':''}><strong>${esc(recordingDate(e))}</strong><small title="${esc(video)}">${esc(meta)}</small></button></div></td><td class="recording-source"><span title="${esc(e.parts.slice(0,-1).join(' / '))}">${esc(sourceLabel)}</span></td><td><div class="recording-outcome" title="${esc(a?.outcome_label||outcome)}">${pill(outcome,outcomeTone)}</div><small class="recording-outcome-detail" title="${esc(a?.confirmation_label||confirmation)}">${esc(confirmation)}</small></td><td><div class="recording-usage" title="${esc(a?.notes_summary||'训练用途需独立审核')}">${pill(usage,usageTone)}</div>${a&&finite(a.transition_count)?`<small class="recording-use-detail">${a.transition_count} 转移${finite(a.retained_seconds)?` · ${fmt(a.retained_seconds,2)} 秒`:''}</small>`:''}</td><td class="data-note-cell"><button type="button" class="data-note-trigger ${note?'':'empty'}" data-note-edit="${esc(e.id)}" draggable="false" aria-label="${note?'编辑':'添加'} ${esc(e.name)} 的备注" ${note?`aria-describedby="note-${esc(e.id)}"`:''} ${dataDeleting?'disabled':''}><span class="data-note-summary">${note?esc(shortNote):'＋ 添加备注'}</span>${note?'<svg class="data-note-pencil" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="m15 5 4 4M4 20l5-1L20 8a2.8 2.8 0 0 0-4-4L5 15Z"/></svg>':''}</button>${note?`<div class="data-note-tooltip" role="tooltip" id="note-${esc(e.id)}">${esc(note)}</div>`:''}</td><td><button class="button small" data-episode="${esc(e.id)}" title="${e.videos?.length?'打开视频回看':'打开录制详情：'+esc(video)}" ${dataDeleting?'disabled':''}>${e.videos?.length?'回看':'查看'}</button></td></tr>`;
 }).join('')}</tbody></table></div>`;
}
function recordingPagination(page){
 const unit=page.folders.length?'项':'条';
 const numbered=new Set([1,page.pages,...Array.from({length:5},(_,i)=>page.page+i-2).filter(n=>n>=1&&n<=page.pages)]);
 let last=0;
 const numbers=[...numbered].sort((a,b)=>a-b).map(n=>{const gap=n-last>1?'<span class="pagination-gap">…</span>':'';last=n;return `${gap}<button class="button small" data-data-page="${n}" ${n===page.page?'aria-current="page"':''} ${dataDeleting?'disabled':''}>${n}</button>`;}).join('');
 return `<div class="data-folder-pagination"><span>显示 ${page.start}–${page.end} ${unit}，共 ${page.total} ${unit}</span>${page.pages>1?`<div><button class="button small" data-data-page="${page.page-1}" ${page.page<=1||dataDeleting?'disabled':''}>上一页</button>${numbers}<button class="button small" data-data-page="${page.page+1}" ${page.page>=page.pages||dataDeleting?'disabled':''}>下一页</button></div>`:''}</div>`;
}
function renderDataCatalog(){
 writeContext('data-filters',dataFilters);dataReturn.folder=app.dataFolder;writeContext('data-return',dataReturn);
 if(app.page!=='data')return;if(!app.data){$('#data-folders').innerHTML=empty('正在读取目录','','data');return;}const data=app.data||{},episodes=data.episodes||[],summary=data.summary||{};
 renderDataStorage();showDataWorkspace();
 $('#episode-count').textContent=`${episodes.length} 个文件夹`;
 const folderMarkup=e=>{const selecting=dataSelecting&&deletable(e),checked=dataSelection.has(e.id);return `<button ${e.source!=='unified_dataset'?`draggable="true" data-drag-episode="${esc(e.id)}"`:''} class="folder-item ${selecting?(checked?'delete-selected':''):app.selectedEpisode===e.id?'selected':''}" ${selecting?`data-select-episode="${esc(e.id)}" role="checkbox" aria-checked="${checked}"`:`data-episode="${esc(e.id)}" aria-pressed="${app.selectedEpisode===e.id}"`} ${dataDeleting?'disabled':''} aria-label="${selecting?'选择':'打开录制'} ${esc(e.name)}">${selecting?`<span class="folder-check" aria-hidden="true">${checked?'✓':''}</span>`:''}<span class="folder-icon" aria-hidden="true"></span><span class="folder-name" title="${esc(e.name)}">${esc(e.name)}</span><span class="folder-meta">${formatBytes(e.size_bytes)} · ${esc(e.file_count??'—')} 个文件</span><span class="folder-meta">${esc(formatDate(e.started_at||e.modified_at,e.display_timezone))}</span>${pill(reviewLabel(e.review?.stale?'pending':e.review?.status),e.review?.status==='approved'?'good':e.review?.status==='rejected'?'warn':'neutral')}</button>`;};
 const view=libraryView(data,app.dataFolder||'');app.dataFolder=view.current||null;const page=folderPage(view,dataFilters);dataFilters.page=page.page;
 $('#episode-count').textContent=`${view.records.length} 条录制`;

 const timeline=dataFilters.browseMode==='time';
 $('#data-library-view').classList.add('data-list-view');$('#data-library-view').classList.toggle('data-time-view',timeline);
 $$('[data-browse-mode]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.browseMode===dataFilters.browseMode)));
 const search=$('#data-search');if(search&&search.value!==dataFilters.query)search.value=dataFilters.query;
 const selectable=page.leaves.filter(e=>deletable(e));
 const selectActions=(selectable.length||dataSelecting)?`<div class="data-select-actions">${dataSelecting?`<button class="button small" data-click="data-select-all" ${dataDeleting||!selectable.length?'disabled':''}>${selectable.length&&selectable.every(e=>dataSelection.has(e.id))?'取消本页':'全选本页'}</button><button class="button small danger" data-click="data-delete" ${!dataSelection.size||dataDeleting||dataDeleteUncertain?'disabled':''}>${dataDeleting?'删除中…':`删除 (${dataSelection.size})`}</button><button class="button small" data-click="data-select-cancel" ${dataDeleting?'disabled':''}>取消</button>`:`<button class="button small primary" data-click="data-select-start">选择</button>`}</div>`:'';
 const crumbs=[`<button class="button small" data-folder="">全部文件夹</button>`];
 for(const crumb of view.breadcrumbs)crumbs.push(`<span>/</span><button class="button small" data-folder="${esc(crumb.path)}">${esc(crumb.name)}</button>`);
 const folders=page.folders.map(f=>`<button class="folder-item ${f.custom?'version-folder':''}" data-folder="${esc(f.path)}" ${f.custom?`draggable="true" data-drag-folder="${esc(f.path)}"`:''}><span class="folder-icon" aria-hidden="true"></span><span class="folder-name">${esc(f.name)}</span><span class="folder-meta">${f.count} 条录制 · ${formatBytes(f.bytes)}</span></button>`);
 const exports=view.exports.length?`<details class="original-frame-review"><summary>训练导出 · ${view.exports.length} 份</summary><p class="section-note">由原始录制转换的 LeRobot 数据集，不重复计作录制；候选仍需审核。</p><div class="folder-grid">${view.exports.map(folderMarkup).join('')}</div></details>`:'';
 const selectedList=dataSelection.size?`<details class="data-selection-summary"><summary>已选 ${dataSelection.size} 条 · 当前结果 ${page.matching.filter(e=>dataSelection.has(e.id)).length} 条</summary><div class="data-selected-list">${[...dataSelection.values()].map(e=>`<div><span>${esc(e.name)}${episodes.find(row=>row.id===e.id)?.source_signature!==e.source_signature?' · 来源失效':''}</span><button class="button small" data-unselect="${esc(e.id)}" ${dataDeleting?'disabled':''}>取消选择</button></div>`).join('')}</div><button class="button small" data-click="data-select-clear" ${dataDeleting?'disabled':''}>清空选择</button></details>`:'';
 const deletion=dataDeleteResult?`<div class="notice ${dataDeleteUncertain?'warn':''}" role="status">${esc(dataDeleteResult.text)}${dataDeleteResult.transaction?` · 记录 ${esc(dataDeleteResult.transaction)}`:''}${dataDeleteResult.results.map(r=>`<p>${esc(r.name||r.id)} · ${esc(r.error||r.state)}</p>`).join('')}</div>`:'';
 const actions=$('#data-header-actions');if(actions&&actions.innerHTML!==selectActions)actions.innerHTML=selectActions;
 const browserToolbar=timeline?`<div class="data-browser-toolbar"><span class="section-note">全部录制</span>${dataOrganization.actions()}</div>`:`<div class="data-browser-toolbar"><nav class="data-breadcrumb" aria-label="数据文件夹">${crumbs.join('')}</nav>${dataOrganization.actions()}</div>`;
 const html=`${browserToolbar}${dataOrganization.feedback()}${selectedList}${deletion}${folderSummaryHTML(view)}${timeline?'':rltSummaryHTML(view)}${recordingListHeading(view,page)}${folders.length?`<div class="folder-grid">${folders.join('')}</div>`:''}${page.leaves.length?recordingTableHTML(page.leaves,view,page):!folders.length?empty(dataFilters.query||dataFilters.sourceMode?'没有匹配的录制':'暂无录制','录制结束并保存后显示。','data'):''}${recordingPagination(page)}${!timeline&&!view.current?exports:''}`;
 const holder=$('#data-folders');if(holder._f1CatalogMarkup!==html){holder._f1CatalogMarkup=html;const open=holder.querySelector('.data-selection-summary')?.open;holder.innerHTML=html;if(open&&holder.querySelector('.data-selection-summary'))holder.querySelector('.data-selection-summary').open=true;}
 dataCovers.mount(holder,episodes);
 dataOrganization.renderTree();
 if(app.episodeDetail&&app.selectedEpisode===app.episodeDetail.id)renderEpisodeDetail();
}
async function selectEpisode(id){
 const generation=++dataDetailGeneration,holder=$('#episode-detail');dataNavigation++;app.dataDetailIssue=null;disposeMediaViewers(holder);app.selectedEpisode=id;app.episodeDetail=null;app.reviewDirty=dataReviewDrafts.has(id);resetDataSelection();showDataWorkspace();delete holder.dataset.episodeSignature;
 holder.innerHTML=`<button class="button small data-back" data-click="data-back">← 返回文件夹</button><section class="card">${empty('正在打开录制','','data')}</section>`;
 holder.scrollIntoView({block:'start'});
 try{const result=await api(`/api/data/episodes/${encodeURIComponent(id)}`);if(generation!==dataDetailGeneration||app.selectedEpisode!==id||app.page!=='data')return;app.episodeDetail=result.episode;if(!dataQueue.includes(id)){dataQueue=[id];writeContext('data-queue',dataQueue);}renderEpisodeDetail();}catch(error){if(generation===dataDetailGeneration&&app.page==='data'&&app.selectedEpisode===id)holder.innerHTML=`<button class="button small data-back" data-click="data-back">← 返回文件夹</button><section class="card">${empty('无法打开录制',error.message,'data')}</section>`;}
}
function renderEpisodeDetail(){
 const e=app.episodeDetail;if(app.page!=='data'||!e)return;const holder=$('#episode-detail'),signature=detailKey(e);showDataWorkspace();if(holder.dataset.episodeSignature===signature){updateReviewStatus();refreshRLTDetail(e);return;}holder.dataset.episodeSignature=signature;disposeMediaViewers(holder);
 const draft=dataReviewDrafts.get(e.id),review={...(e.review||{}),...(draft||{})},topics=e.topics||[];
 holder.innerHTML=`${reviewNavigation()}<section class="card data-approved-detail ${e.capture_mode==='rlt'?'data-rlt-detail':''}"><div class="episode-heading" data-recording-name="${esc(e.name)}"><div><h2>${esc(formatDate(e.started_at||e.modified_at,e.display_timezone))}</h2><p>${esc(e.capture_label||(e.recording_kind==='rollout'?'真机 rollout':({hil_demonstration:'人工示教',hil_policy_hil:'策略 HIL',pico_capture:'Pico 遥操',lxl:'LXL',lerobot:'Pico 录制',unified_dataset:'训练导出'}[e.source]||'录制')))} · ${formatBytes(e.size_bytes)}${finite(e.duration_s)?` · ${fmt(e.duration_s,1)} 秒`:''}</p></div></div><div class="data-detail-tabs"><button type="button" data-video-tab aria-selected="true">视频回看</button>${e.source!=='unified_dataset'?'<button type="button" data-motion-replay-open aria-selected="false" aria-expanded="false">机器人复现 <small>Replay</small></button>':''}</div>${motionReplayHTML(e)}<div class="data-review-workspace" data-video-workspace><div class="data-review-main">${episodeVideos(e.videos,e)}<div data-rlt-range>${rltRangeHTML(e)}</div></div><form class="review-form" id="review-form"><div data-rlt-assessment>${rltDetailHTML(e)}</div><h3>备注</h3><p id="data-detail-notice" class="notice warn" hidden></p><label class="data-notes-label"><span class="sr-only">录制备注</span><textarea id="review-notes" aria-label="录制备注" maxlength="4000" placeholder="布局变化、值得留意的片段…">${esc(review.notes||'')}</textarea></label><button data-review-save type="submit" class="button primary" ${dataReviewSaving.has(e.id)?'disabled':''}>${dataReviewSaving.has(e.id)?'保存中…':'保存备注'}</button><button type="button" class="button small" data-click="data-review-discard" ${draft?'':'hidden'} ${dataReviewSaving.has(e.id)?'disabled':''}>放弃草稿</button><span id="review-saved" class="review-note">${draft?'未保存':(review.notes_updated_at||review.updated_at)?`上次保存 ${esc(formatDate(review.notes_updated_at||review.updated_at))}`:''}</span><section class="review-time-notes"><h4>时间点备注</h4><div data-timestamp-notes>${timestampNotesHTML(review.notes)}</div></section></form></div><details class="data-review-optional"><summary>审核记录（可选）</summary><div class="data-review-optional-body">${savedReviewBadge(e)}<label class="review-select-label">审核结论<select id="review-status">${[['pending','待审核'],['approved','通过'],['rejected','需排查']].map(([value,label])=>`<option value="${value}" ${review.status===value?'selected':''}>${label}</option>`).join('')}</select></label><div class="review-status-buttons">${[['pending','待审核'],['approved','通过'],['rejected','需排查']].map(([v,l])=>`<button type="button" data-review-choice="${v}" aria-pressed="${review.status===v}">${l}</button>`).join('')}</div><button data-review-save type="button" class="button small" data-click="data-review-commit">保存审核结果</button></div></details><details class="data-technical"><summary>技术信息 · 存储路径、消息主题与文件明细</summary><div class="technical-layout"><div class="episode-path"><h3>存储路径</h3><p>${esc(e.name)}</p><code>${esc(e.path||e.relative_path||'未报告')}</code><h3>消息主题 · ${topics.length}</h3><div class="topic-list">${topics.map(topic=>`<div class="topic-entry"><span>${esc(topic.name)}</span><small>${esc(topic.type)} · ${esc(topic.message_count??'—')} 条</small></div>`).join('')||'<p class="muted">没有可解析的主题信息</p>'}</div></div><div class="episode-files"><h3>文件明细 · ${(e.files||[]).length}</h3><div class="table-scroll episode-files-scroll"><table class="data-table"><thead><tr><th>文件名</th><th>大小</th><th>更新时间</th></tr></thead><tbody>${(e.files||[]).map(file=>`<tr><td>${esc(file.relative_path||file.name)}</td><td>${formatBytes(file.size_bytes)}</td><td>${esc(formatDate(file.modified_at))}</td></tr>`).join('')||'<tr><td colspan="3">没有可显示的文件</td></tr>'}</tbody></table></div></div></div></details></section>`;
 const range=holder.querySelector('[data-rlt-range]'),joints=holder.querySelector('[data-joint-overview]');if(e.capture_mode==='rlt'&&range&&joints)joints.before(range);
 $('#review-form').addEventListener('submit',event=>{event.preventDefault();run(()=>saveReview(false,true));});
 $$('[data-review-choice]',holder).forEach(b=>b.onclick=()=>{$('#review-status').value=b.dataset.reviewChoice;cacheDataReviewDraft();});
 updateReviewStatus();mountMediaViewers(holder);
 const motionRoot=holder.querySelector('[data-motion-replay]');if(motionRoot){motionReplay=new MotionReplay(motionRoot,e,{api,run,acquire,release,stop,intentGeneration:()=>motionIntent,expanded:open=>{const entry=holder.querySelector('[data-motion-replay-open]');entry.setAttribute('aria-expanded',String(open));entry.setAttribute('aria-selected',String(open));holder.querySelector('[data-video-tab]').setAttribute('aria-selected',String(!open));holder.querySelector('[data-video-workspace]').hidden=open;if(open)activeMediaPlayers.forEach(p=>p.pause?.());},online:()=>app.apiOnline&&app.state?.connected===true,source:()=>app.episodeDetail?.source_signature,pauseHistory:()=>activeMediaPlayers.forEach(p=>p.pause?.()),state:()=>app.state,lease:()=>ownLease()?app.lease:null,executing:()=>app.executing,refresh:refreshState,accept:action=>{app.activeAction=action;app.submittedDraft=null;app.actionContext={id:action.id,title:'实机 Replay',estimatedTotal:null};}});holder.querySelector('[data-motion-replay-open]').onclick=()=>motionReplay.open();holder.querySelector('[data-video-tab]').onclick=()=>motionReplay.q('[data-r-close]')?.click();}
}
async function saveReview(next=false,notesOnly=false){
 const id=app.selectedEpisode,nav=dataNavigation;if(!id||dataReviewSaving.has(id)||app.dataDetailIssue)return;
 if(!app.authenticated&&!await login())return;if(id!==app.selectedEpisode||nav!==dataNavigation)return;
 cacheDataReviewDraft();const snapshot={...dataReviewDrafts.get(id)},source=app.episodeDetail?.source_signature,catalogSource=app.data?.episodes?.find(e=>e.id===id)?.source_signature;
 if(snapshot.source_signature!==source){updateReviewStatus();return;}
 dataReviewSaving.add(id);updateReviewStatus();$('#review-saved').textContent='保存中…';
 try{
  const result=await api(notesOnly?'/api/data/notes':'/api/data/reviews',{episode_id:id,...(notesOnly?{}:{status:snapshot.status}),notes:snapshot.notes,expected_source_signature:source,expected_review_revision:snapshot.review_revision});
  const known=app.data?.episodes?.find(e=>e.id===id),detailSource=app.episodeDetail?.id===id?app.episodeDetail.source_signature:null;
  const sourceReplaced=detailSource?detailSource!==source:catalogSource===source&&known&&known.source_signature!==source;
  const pending=dataReviewDrafts.get(id),newer=sourceReplaced||pending?.version!==snapshot.version||pending?.source_signature!==source;
  if(!newer)dataReviewDrafts.delete(id);else if(!sourceReplaced&&pending?.source_signature===source)pending.review_revision=result.review.revision;
  persistReviewDrafts();const current=app.selectedEpisode===id&&app.page==='data'&&app.episodeDetail?.source_signature===source;
  if(app.selectedEpisode===id&&app.episodeDetail?.source_signature===source){app.episodeDetail.review=result.review;app.reviewDirty=newer;}
  if(current){$('#review-saved').textContent=newer?'未保存':`已保存 ${formatDate(result.review.notes_updated_at||result.review.updated_at)}`;updateReviewStatus();}
  const row=app.data?.episodes?.find(e=>e.id===id);if(row&&row.source_signature===source)row.review=result.review;
  if(next&&current&&!newer&&nav===dataNavigation)await adjacentEpisode(1);else toast(notesOnly?'备注已保存':'审核已保存');
 }catch(error){
  if(app.selectedEpisode===id&&app.page==='data'){
   if(error.code==='SOURCE_CHANGED')app.dataDetailIssue='来源已更新，请重新读取后编辑。';
   if(error.payload?.review)app.episodeDetail.review=error.payload.review;
   $('#review-saved').textContent=error.message;updateReviewStatus();
  }
  toast(error.message,true);
 }finally{dataReviewSaving.delete(id);if(app.selectedEpisode===id&&app.page==='data')updateReviewStatus();}
}
function renderEvents(holder){if(!holder)return;holder.innerHTML=app.events.length?[...app.events].slice(0,40).map(event=>{const stamp=event.at||event.time||event.timestamp||event.created_at;const date=stamp?new Date(typeof stamp==='number'&&stamp<1e12?stamp*1000:stamp):null;const time=date&&!Number.isNaN(date.getTime())?date.toLocaleTimeString('zh-CN',{hour12:false}):'—';return `<div class="event"><time>${esc(time)}</time><p><span class="event-type">${esc(event.type||event.kind||'事件')}</span>${esc(event.message||event.detail||event.error||event.action_id||'已记录')}</p></div>`;}).join(''):empty('暂无操作记录','动作结果和控制权变化会记录在这里。');}
function updateSystem(){
 if(!$('#workbench-drawer')?.open)return;const r=app.robot||{},s=app.system||{};
 const items=[['机器人',r.id||'未读取'],['机器标签',r.label||'未配置'],['局域网 IP',r.ip||'未读取'],['服务版本',s.version||r.version||'未读取'],['运行模式',s.mode||'以本机配置为准'],['消息 / 环境',s.ros_distro||s.ros?.distro||'未报告']];
 if($('#system-identity'))$('#system-identity').innerHTML=items.map(([key,value])=>`<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join('');
 if($('#vendor-link')&&r.ip&&/^[\d.]+$/.test(r.ip))$('#vendor-link').href=`http://${r.ip}/`;
 $$('[data-click=login]', $('#workbench-drawer')).forEach(b=>b.hidden=app.authenticated);$$('[data-click=logout]', $('#workbench-drawer')).forEach(b=>b.hidden=!app.authenticated);
 if($('#session-heading'))$('#session-heading').textContent=app.authenticated?'操作会话已启用':'当前为查看模式';
 if($('#workbench-drawer [data-click=release]'))$('#workbench-drawer [data-click=release]').hidden=!app.lease;
 if($('#session-status'))$('#session-status').innerHTML=lineList([['访问',app.authenticated?'已启用操作':'只读观察'],['控制权',app.lease?'由当前浏览器持有':app.state?.control?.owner||'未持有'],['可用情况',app.state?.control?.reason||'以实际请求检查为准']]);
 const services=app.state?.services||s.services||{};if($('#system-services'))$('#system-services').innerHTML=Object.keys(services).length?Object.entries(services).map(([name,entry])=>{const status=statusFor(entry,'service');return `<div class="line-item"><span>${esc(name)}</span>${pill(status.text,status.type)}</div>`;}).join(''):empty('尚无服务清单','等待本机服务回报运行状态。');
 if($('#revision-label'))$('#revision-label').textContent=app.revision?String(app.revision).slice(0,18):'等待检测';renderEvents($('#system-events'));
}
function updatePanels(){motionReplay?.update();updateOverview();updateControl();updatePico();updateData();renderRecording();updateSystem();}
function updateGlobal(){
 updateStartup();updateSystem();
 const s=app.state,connected=app.apiOnline;$('#connection-pill').className=`pill ${connected?'good':'neutral'}`;$('#connection-pill').innerHTML=`<span class="small-dot"></span>${connected?'服务在线':'未连接'}`;$('#sidebar-status').textContent=connected?'本地网关已连接':'等待本地服务';$('#sidebar-dot').style.color=connected?'#a9d79a':'#849087';$('#robot-label').textContent=app.robot?.label||app.robot?.id||'F1';$('#updated-at').textContent=app.lastReceived?`最近同步 ${new Date(app.lastReceived).toLocaleTimeString('zh-CN',{hour12:false})}`:'尚未同步';
 $('#stop-button').disabled=!ownLease()&&!app.executing&&!app.preflighting;
 const boundary=headBoundaryPhase()&&actionRunning();
 $('#stop-button').textContent=app.page==='hil'?'停止工作台动作':boundary?'取消后续动作':'停止当前动作';
 $('#stop-button').title=boundary?'取消后续动作；不能撤回驱动已接收的头部边界目标':'只停止本工作台拥有的动作，不代替硬件急停';
 $$('[data-click="stop"]').forEach(el=>{el.textContent=boundary?'取消后续动作':'停止动作';el.title=boundary?'不能撤回已接收的头部边界目标':'';});
 document.body.classList.toggle('control-mode',ownLease());if($('#control-mode-exit'))$('#control-mode-exit').hidden=!ownLease();
 if($('#pose-control-status'))$('#pose-control-status').hidden=!ownLease();
 const busy=poseBusy();$$('[data-target-input],[data-target-slider],[data-waist-delta],[data-waist-step],[data-preset-index],[data-preset-delete],[data-click="align"],[data-click="new-preset"]').forEach(el=>{const name=el.dataset.targetSlider,range=name?.startsWith('gripper_')?[0,100]:app.robot?.joint_limits_deg?.[name];el.disabled=busy||Boolean(name&&(!Array.isArray(range)||app.draft[name]===undefined||app.draft[name]===''));});
 $$('[data-execute-required]').forEach(el=>{el.disabled=busy||actionRunning(s?.action)||!connected||!s?.connected||(!ownLease()&&Boolean(s?.control?.owner))||(app.group!=='base'&&!app.dirty.size&&app.waistIntent===null);el.innerHTML=actionRunning()?'执行中…':app.preflighting?'检查中…':`执行 ${icon('arrow')}`;});
 $$('[data-click="stop"]').forEach(el=>el.disabled=!ownLease()&&!app.executing&&!app.preflighting);
 const awaitingDriver=connected&&s?.services?.motor_ctl?.running===false&&!Object.keys(s?.joints||{}).length;const warning=!connected?'工作台服务暂不可达，当前显示的是最后收到的状态。':s?.error?String(s.error):awaitingDriver?'网页已启动，但尚未检测到本体关节驱动。当前关节状态与姿态控制不可用，请检查机器人的基础启动流程。':'';$('#global-alert').hidden=!warning;$('#global-alert').textContent=warning;
 if($('#lease-heading')){
   $('#lease-heading').textContent=ownLease()?'当前浏览器持有控制权':s?.control?.owner?`当前控制者：${s.control.owner}`:'观察模式';
   $('#lease-description').textContent=ownLease()?'操作心跳已建立。释放后可切换至现场 Pico；失联后由本机租约期限处理。':s?.control?.reason||'编辑目标后点击执行即可开始运动。';
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
 finally{app.stateBusy=false;updateGlobal();motionReplay?.update();}
}
async function refreshPresets(){const result=await api('/api/presets');app.presets=Array.isArray(result.presets)?result.presets:[];renderPresets();}
async function refreshEvents(){try{const result=await api('/api/events');app.events=Array.isArray(result.events)?result.events:[];for(const id of ['event-list','system-events'])renderEvents($(`#${id}`));}catch{}}
async function refreshSystem(){try{const [result,identity]=await Promise.all([api('/api/system'),api('/api/robot')]);app.system=result.system||result;app.robot=identity.robot;updateSystem();updatePico();updateGlobal();}catch{}}
async function heartbeat(){if(!app.lease)return;try{await api('/api/control/heartbeat',{lease_id:app.lease});}catch(error){app.lease=null;stopJog();updateGlobal();motionReplay?.update();toast(`控制权已释放：${error.message}`,true);}}
async function login(){
 if(!app.authenticated){const result=await api('/api/session');app.authenticated=result.authenticated===true;updateGlobal();}
 return app.authenticated;
}
let acquisition=null,motionIntent=0;
async function acquire(){
 if(ownLease())return true;
 if(acquisition)return acquisition;
 const generation=motionIntent;
 acquisition=(async()=>{const result=await api('/api/control/acquire',{owner:'browser'});if(generation!==motionIntent){await api('/api/control/release',{lease_id:result.lease_id});return false;}app.authenticated=true;app.lease=result.lease_id;updateGlobal();return true;})();
 try{return await acquisition;}finally{acquisition=null;}
}
async function release(){motionIntent++;await stopJog();if(app.lease){const lease=app.lease;await api('/api/control/release',{lease_id:lease});app.lease=null;toast('已释放控制权');}updateGlobal();await refreshState();}
function readTargets(names){const targets={};for(const name of names){const value=app.draft[name];if(value!==''&&value!==undefined&&value!==null){const number=Number(value);if(!Number.isFinite(number))throw new Error(`${labelJoint(name)}目标不是有效数字`);const range=app.robot?.limits?.[name];if(Array.isArray(range)&&(number<range[0]||number>range[1])&&!app.dirty.has(name))continue;if(Array.isArray(range)&&(number<range[0]||number>range[1]))throw new Error(`${labelJoint(name)}目标超出本机配置范围 ${range[0]}～${range[1]}°`);targets[name]=number;}}if(!Object.keys(targets).length)throw new Error('请先输入目标，或从新鲜实测填入');return targets;}
async function previewAction(action){
 if(!app.apiOnline||!app.state?.connected)throw new Error('请等待机器人反馈接入');
 if(app.executing||actionRunning())return;
 app.executing=true;updateGlobal();
 // Snapshot the operator's click. No polling, retry or page load submits motion.
 const intent=structuredClone(action),generation=++motionIntent;
 try{
  if(!ownLease())await acquire();
  if(generation!==motionIntent)return;
  if(intent.kind==='base_authorize'){toast('操控模式已启用，请按住方向键移动，松手即停');return;}
  if(['joints','lift','waist_height','pose'].includes(intent.kind)){
   const response=await api('/api/actions/plan',{...intent,lease_id:app.lease});
   if(headBoundaryPlan(response.plan))intent.head_boundary_recovery_ack=true;
  }
  if(generation!==motionIntent)return;
  await executeAction(intent);
  if(generation!==motionIntent&&app.lease)await stop();
 }catch(error){app.poseError=error.message;updatePoseSummary();throw error;}
 finally{app.executing=false;updateGlobal();}
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
 const draft=$('#pose-draft-scope');if(draft)draft.textContent=app.dirty.size||app.waistIntent!==null?`待执行 · ${app.dirty.size+(app.waistIntent!==null?1:0)} 项 · ${labels.concat(app.waistIntent!==null?['升降']:[]).join('、')}`:'尚未编辑目标';
 const current=app.activeAction,context=app.actionContext?.id===current?.id?app.actionContext:null,p=app.posePlan;
 const currentName=context?.title||'姿态调整',estimate=context?.estimatedTotal??estimatedTime(p),running=actionRunning(current);
 const error=app.poseError||current?.error||'',atTarget=completedPoseStillCurrent(current,context),external=app.state?.action?.id!==current?.id?app.state?.action:null;
 title.textContent=running?`正在${currentName==='全身复位'||currentName==='双臂复位'?'复位':'调整姿态'}`:atTarget?`${currentName} · 已到位`:verifiedAction(current)?`上次${currentName}已完成`:labels.length?pendingTitle():'调整姿态';
 let summary=error?briefPoseError(error,current?.result?.commands_sent===true):running?`${finite(estimate)?`预计 ${fmt(estimate,1)} 秒 · `:''}${context?.headBoundaryRecovery?'正在执行':'正在执行，可随时停止'}`:atTarget?'所有目标已确认到位':verifiedAction(current)?'当前姿态已变化或反馈待更新，请以实测为准':current?.status==='succeeded'?'执行已结束，尚未确认到位':current&&['failed','rejected','cancelled','stopped','completed'].includes(current.status)?`${actionLabel(current.status)}${current.result?.commands_sent?'，再次执行将从实测位置重新规划':'，目标保留'}`:p?.no_motion?'目标与当前姿态接近，执行后确认到位':p?`${labels.join('、')} · 预计 ${fmt(estimatedTime(p),1)} 秒`:labels.length?'正在检查目标…':app.poseNotice||'选择关节目标，或载入上方预设';
 if(!error&&!running&&p?.head_boundary_recovery)summary='头部需先回到工作范围 · 点击执行将包含回程';
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
 const generation=motionIntent;clearTimeout(poseTimer);poseTimer=null;app.poseSeq++;app.preflighting=true;updateGlobal();
 try{app.poseError=null;await refreshPosePlan();if(app.poseError)throw new Error(app.poseError);if(!app.posePlan)throw new Error('请先调整姿态或载入预设');
 if(generation!==motionIntent)return;return await previewAction(pendingPose());}finally{app.preflighting=false;updateGlobal();}
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
async function stop(){motionIntent++;await stopJog();if(!app.lease)return;const result=await api('/api/stop',{lease_id:app.lease});if(result.action)app.activeAction=result.action;updateControl();await refreshState();}
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
async function deletePreset(index){const preset=app.presets[index];if(!preset)return;if(!await login())return;await api('/api/presets/delete',{id:preset.id});await refreshPresets();toast('姿态已删除');}
const clicks={'system-details':()=>openWorkbenchPanel('system'),'startup-details':()=>openWorkbenchPanel('startup'),'session-details':()=>openWorkbenchPanel('session'),'close-workbench':()=>$('#workbench-drawer')?.close(),'copy-workbench':()=>copyWorkbench(),'copy-startup':()=>copyWorkbench(true),'recording-refresh':refreshRecording,'data-save-next':()=>saveReview(true),'data-review-commit':()=>saveReview(false),'data-previous':()=>adjacentEpisode(-1),'data-next':()=>adjacentEpisode(1),'data-review-reconcile':reconcileReview,'data-select-clear':()=>{dataSelection.clear();renderDataCatalog();},'data-select-start':()=>{dataSelecting=true;renderDataCatalog();},'data-select-cancel':()=>{resetDataSelection();renderDataCatalog();},'data-back':returnDataLibrary,'data-review-discard':discardDataReviewDraft,'data-select-all':()=>{const rows=dataLibraryPage().leaves.filter(e=>deletable(e));if(rows.every(e=>dataSelection.has(e.id)))for(const e of rows)dataSelection.delete(e.id);else for(const e of rows){if(dataSelection.size>=100)break;dataSelection.set(e.id,{id:e.id,source_signature:e.source_signature,name:e.name,size_bytes:e.size_bytes});}renderDataCatalog();},'data-delete':deleteSelectedData,'pico-recording':()=>showPicoView('record'),stop,'execute-current':executeCurrent,'focus-model':()=>{const focused=document.body.classList.toggle('model-focus');const button=$('[data-click="focus-model"]');if(button)button.textContent=focused?'返回调姿':'放大模型';app.viewer?.resize();},'toggle-nav':()=>{document.body.classList.toggle('nav-collapsed');try{localStorage.setItem('f1-nav-collapsed',document.body.classList.contains('nav-collapsed')?'1':'0');}catch{}$('#toggle-navigation').setAttribute('aria-expanded',String(!document.body.classList.contains('nav-collapsed')));app.viewer?.resize();},'startup':startBase,'new-preset':newPreset,login,acquire,release,'execute-joints':()=>previewAction({kind:'joints',targets:readTargets(groupNames())}),'execute-lift':()=>{if(app.liftPercent===null)throw new Error('请先拖动升降滑条或选择快捷位置');return previewAction(app.liftLevel?{kind:'lift',level:app.liftLevel}:{kind:'joints',targets:readTargets(liftNames)});},align:()=>{app.waistPending=false;app.waistError=null;app.waistIntent=null;app.waistPreview=null;app.waistDelta=0;app.waistPreviewSeq++;app.activePresetId=null;app.dirty.clear();app.activeAction=null;app.actionContext=null;seedLiveTargets();invalidatePose();persist();renderControlEditor();renderPresets();},capture:()=>savePreset(true),'save-draft':()=>savePreset(false),import:()=>$('#preset-file').click(),export:exportPresets,'refresh-data':refreshData,refresh:async()=>{await Promise.all([refreshState(),refreshSystem(),refreshStartup(true)]);toast('已刷新');},logout:async()=>{await release();await api('/api/logout',{});updateGlobal();updateSystem();}};
document.addEventListener('click',event=>{
 const note=event.target.closest('[data-note-seek]');if(note){const player=[...activeMediaPlayers].find(p=>p.root.closest('#episode-detail'));if(player){player.pause();player.seek(Number(note.dataset.noteSeek));}else toast('视频尚未就绪');return;}
 const variant=event.target.closest('[data-pico-variant]');if(variant){selectPicoVariant(variant.dataset.picoVariant);return;}
 const picoView=event.target.closest('[data-pico-view]');if(picoView){showPicoView(picoView.dataset.picoView);return;}
 const systemTab=event.target.closest('[data-system-tab]');if(systemTab){openWorkbenchPanel('system',systemTab.dataset.systemTab);$('#workbench-drawer [aria-selected=true]')?.focus();return;}
 const wm=event.target.closest('[data-waist-mode]');if(wm){app.waistMode=wm.dataset.waistMode;app.waistPreview=null;app.waistPreviewSeq++;renderControlEditor();return;}
 const step=event.target.closest('[data-waist-step]');if(step){editWaistDelta(step.dataset.waistStep);return;}
 const lift=event.target.closest('[data-lift-level]');if(lift){if(lift.disabled)return;editLiftPercent(presetPercent(Number(lift.dataset.liftLevel),app.robot?.joint_limits_deg));app.liftLevel=Number(lift.dataset.liftLevel);return;}
 const teleop=event.target.closest('[data-teleop]');if(teleop&&!teleop.disabled){run(()=>teleopAction(teleop.dataset.teleop,teleop.dataset.variant));return;}
 const recordingButton=event.target.closest('[data-recording]');if(recordingButton&&!recordingButton.disabled){run(()=>recordingAction(recordingButton.dataset.recording));return;}
 const removeData=event.target.closest('[data-unselect]');if(removeData){dataSelection.delete(removeData.dataset.unselect);renderDataCatalog();return;}
 const selectedData=event.target.closest('[data-select-episode]');if(selectedData){toggleDataSelection(selectedData.dataset.selectEpisode);return;}
 const noteEntry=event.target.closest('[data-note-edit]');if(noteEntry&&!noteEntry.disabled&&!dataDeleting){const row=app.data?.episodes?.find(e=>e.id===noteEntry.dataset.noteEdit);if(row)dataNotesEditor.open(row);return;}
 const replayEntry=event.target.closest('[data-episode-replay]');if(replayEntry&&!replayEntry.disabled){run(async()=>{await openDataEpisode(replayEntry.dataset.episodeReplay);motionReplay?.open();});return;}
 const episode=event.target.closest('[data-episode]');if(episode){run(()=>openDataEpisode(episode.dataset.episode));return;}
 const nav=event.target.closest('[data-nav]');if(nav){stopJog();location.hash=nav.dataset.nav==='data'&&app.selectedEpisode?'data?episode='+app.selectedEpisode:nav.dataset.nav;return;}
 const browse=event.target.closest('[data-browse-mode]');if(browse){if(dataDeleting||dataOrganization.engaged)return;if(app.selectedEpisode)returnDataLibrary();resetDataSelection();dataFilters.browseMode=browse.dataset.browseMode;dataFilters.page=1;dataFilters.query='';dataFilters.sourceMode='';dataFilters.assessmentFilter='';renderDataCatalog();return;}
 const assessmentFilter=event.target.closest('[data-assessment-filter]');if(assessmentFilter){if(dataDeleting)return;resetDataSelection();dataFilters.assessmentFilter=assessmentFilter.dataset.assessmentFilter;dataFilters.page=1;renderDataCatalog();return;}
 const folder=event.target.closest('[data-folder]');if(folder){if(dataDeleting||dataOrganization.engaged)return;if(app.selectedEpisode)returnDataLibrary();resetDataSelection();dataFilters.page=1;dataFilters.assessmentFilter='';dataFilters.sourceMode='';dataFilters.query='';dataFilters.browseMode='folders';app.dataFolder=folder.dataset.folder||null;renderDataCatalog();return;}
 const dataPageButton=event.target.closest('[data-data-page]');if(dataPageButton){if(dataDeleting||dataPageButton.disabled)return;dataFilters.page=Number(dataPageButton.dataset.dataPage);renderDataCatalog();return;}
 const part=event.target.closest('[data-pose-part]');if(part){selectPosePart(part.dataset.posePart);return;}
 const group=event.target.closest('[data-group]');if(group){stopJog();app.group=group.dataset.group;if(app.group==='lift'){app.liftLevel=null;app.liftPercent=null;app.waistDelta=0;app.waistPreview=null;app.waistPreviewSeq++;}persist();renderPage();return;}
 const button=event.target.closest('[data-click]');if(button&&!button.disabled){run(async()=>{button.disabled=true;try{await clicks[button.dataset.click]?.();}finally{button.disabled=false;updateGlobal();if(app.page==='data')updateReviewStatus();}});return;}
 const exp=event.target.closest('[data-preset-export]');if(exp){run(()=>exportPresets(Number(exp.dataset.presetExport)));return;}
 const del=event.target.closest('[data-preset-delete]');if(del&&!del.disabled&&!poseBusy()){run(()=>deletePreset(Number(del.dataset.presetDelete)));return;}
 const presetButton=event.target.closest('[data-preset-index]');if(presetButton&&!presetButton.disabled&&!poseBusy()){const preset=app.presets[Number(presetButton.dataset.presetIndex)];app.liftPercent=null;app.liftLevel=null;app.draft={...preset.joints};app.dirty=new Set(Object.keys(app.draft));for(const [side,value] of Object.entries(preset.grippers||{})){app.draft[`gripper_${side}`]=value;app.dirty.add(`gripper_${side}`);}app.activePresetId=preset.id;app.presetName=preset.name;app.group='pose';app.waistPending=false;app.waistError=null;app.waistIntent=null;app.waistPreview=null;app.waistPreviewSeq++;app.activeAction=null;invalidatePose();persist();renderControlEditor();renderPresets();updateControl();return;}
 const gripper=event.target.closest('[data-gripper-execute]');if(gripper&&!gripper.disabled)run(()=>{const side=gripper.dataset.gripperExecute,raw=app.draft[`gripper_${side}`],position=Number(raw);if(raw===''||raw===undefined||!Number.isFinite(position)||position<0||position>100)throw new Error('请输入 0–100 之间的夹爪位置');return previewAction({kind:'gripper',side,position});});
});
document.addEventListener('change',event=>{if(event.target.dataset.dataFilter){if(dataDeleting||dataOrganization.engaged)return;resetDataSelection();dataFilters[event.target.dataset.dataFilter]=event.target.value;dataFilters.page=1;renderDataCatalog();return;}if(event.target.id==='whole-part'){app.wholePart=event.target.value;renderControlEditor();}});
document.addEventListener('focusout',event=>{const input=event.target,name=input.dataset.targetInput;if(name)input.value=targetDisplay(name,app.draft[name]);});
document.addEventListener('input',event=>{if(event.target.id==='data-search'){dataFilters.query=event.target.value;dataFilters.page=1;renderDataCatalog();return;}if(event.target.hasAttribute('data-waist-delta')){editWaistDelta(event.target.value);return;}if(event.target.hasAttribute('data-lift-percent')){editLiftPercent(event.target.value);return;}const input=event.target,name=input.dataset.targetInput||input.dataset.targetSlider;if(name){if(poseBusy())return;clearPresetSelection();if(liftNames.includes(name)){app.waistPending=false;app.waistError=null;app.waistIntent=null;app.waistPreview=null;app.waistPreviewSeq++;if($('#waist-plan-status'))$('#waist-plan-status').textContent='使用当前身体关节目标';}app.draft[name]=input.value;app.dirty.add(name);app.activeAction=null;invalidatePose();}else if(input.id==='preset-name')app.presetName=input.value;else if(input.id==='review-notes'||input.id==='review-status'){cacheDataReviewDraft();return;}else return;persist();updateControl();});
$('#stop-button').addEventListener('click',()=>run(stop));
const entryNotice=$('#entry-notice');
// The lightweight notice is ready before this module and its dependencies.
entryNotice.addEventListener('close',()=>{
 cameraPreview?.sync();
 if(app.page==='control'&&!app.viewer)mountRobotViewer();
 const pending=app.deferredWorkbenchPanel;app.deferredWorkbenchPanel=null;
 if(pending)openWorkbenchPanel(pending.kind,pending.tab);
});
$('#preset-file').addEventListener('change',event=>run(async()=>{const file=event.target.files[0];if(!file)return;try{if(file.size>200*1024)throw new Error('姿态文件超过 200 KB，请检查是否选择正确');const parsed=JSON.parse(await file.text());const presets=Array.isArray(parsed)?parsed:parsed.presets;if(!Array.isArray(presets))throw new Error('文件必须包含 presets 数组');if(!await login())return;await api('/api/presets/import',{presets});await refreshPresets();toast('姿态导入完成');}finally{event.target.value='';}}));
window.addEventListener('hashchange',()=>{stopJog();renderPage();});window.addEventListener('blur',()=>stopJog());document.addEventListener('visibilitychange',()=>{if(document.hidden)stopJog();});
window.addEventListener('pagehide',()=>{motionIntent++;stopJog();if(app.lease)fetch('/api/control/release',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-F1-Request':'1'},body:JSON.stringify({lease_id:app.lease}),keepalive:true}).catch(()=>{});});
async function checkRevision(){try{const result=await api('/api/dev/revision');if(app.revision!==null&&result.revision&&result.revision!==app.revision){persist();await stopJog();if(app.lease)await release();location.reload();return;}app.revision=result.revision??app.revision;if($('#revision-label'))$('#revision-label').textContent=String(app.revision||'未提供').slice(0,18);}catch{}}
async function refreshOverviewTask(){if(app.page!=='overview'||document.hidden)return;try{app.hilOverview=(await api('/api/hil/state')).hil;app.hilOverviewReceived=performance.now();}catch{app.hilOverview=null;}updateOverview();}
async function init(){const results=await Promise.allSettled([api('/api/robot'),api('/api/session')]);if(results[0].status==='fulfilled')app.robot=results[0].value.robot;if(results[1].status==='fulfilled')app.authenticated=Boolean(results[1].value.authenticated);renderPage();refreshState();Promise.allSettled([refreshPresets(),refreshEvents(),checkRevision(),refreshRecording(),refreshTeleop()]);refreshOverviewTask();setInterval(refreshOverviewTask,2000);setInterval(refreshState,750);setInterval(heartbeat,900);setInterval(refreshRecording,1500);setInterval(refreshTeleop,1000);setInterval(pollAction,750);setInterval(refreshEvents,3000);setInterval(()=>{if(app.page==='data'){refreshData();const eid=new URLSearchParams(location.hash.split('?')[1]||'').get('episode');if(/^[a-f0-9]{24}$/.test(eid||'')&&app.selectedEpisode!==eid)selectEpisode(eid);}},10000);setInterval(()=>{if(app.workbenchPanel&&!document.hidden)refreshSystem();},10000);setInterval(()=>{if(app.page==='overview'||app.workbenchPanel==='startup')refreshStartup();},5000);setInterval(checkRevision,2000);}
try{document.body.classList.toggle('nav-collapsed',localStorage.getItem('f1-nav-collapsed')==='1');}catch{}
init();

document.addEventListener("click",event=>{const b=event.target.closest("[data-hil-copy]");if(b)hilPanel.copy(b.dataset.hilCopy);});
setInterval(()=>hilPanel.refresh(),200);

document.addEventListener('click',event=>{for(const kind of ['play','retry','review']){const b=event.target.closest(`[data-hil-${kind}]`);if(b)hilPanel.mediaAction(kind,b.getAttribute(`data-hil-${kind}`));}});

document.addEventListener('click',event=>{if(event.target.closest('[data-hil-finish]'))hilPanel.finish();});

document.addEventListener('change',event=>{if(event.target.id==='review-notes'||event.target.id==='review-status')cacheDataReviewDraft();});

document.addEventListener('keydown',event=>{
 const tab=event.target.closest?.('[data-pose-part]');if(tab&&['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();const tabs=$$('[data-pose-part]'),i=tabs.indexOf(tab),next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(i+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;selectPosePart(tabs[next].dataset.posePart);tabs[next].focus();}
 if(event.key==='Escape'){const panel=$('.data-storage-details[open]');if(panel){panel.open=false;panel.querySelector('summary').focus();}}
});
document.addEventListener('click',event=>{const panel=$('.data-storage-details[open]');if(panel&&!panel.contains(event.target))panel.open=false;});

document.addEventListener('keydown',event=>{
 const tab=event.target.closest?.('[role=tab][data-pico-view],[role=tab][data-system-tab]');if(!tab||!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
 event.preventDefault();const tabs=[...tab.closest('[role=tablist]').querySelectorAll('[role=tab]')],i=tabs.indexOf(tab),next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(i+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
 const target=tabs[next];target.click();if(target.isConnected)target.focus();
});

// Text selection and note editing must not begin a folder drag.
document.addEventListener('dragstart',event=>{if(event.target.closest('.data-note-cell,.data-note-dialog')){event.preventDefault();event.stopImmediatePropagation();}},true);

document.addEventListener('click',event=>{const tab=event.target.closest('[data-context-hil]');if(tab&&!tab.disabled){hilPanel.workspace?.select(tab.dataset.contextHil);cameraPreview?.sync();}for(const popover of document.querySelectorAll('.storage-popover[open]'))if(!popover.contains(event.target))popover.open=false;});
document.addEventListener('keydown',event=>{if(event.key==='Escape')for(const popover of document.querySelectorAll('.storage-popover[open]')){popover.open=false;popover.querySelector('summary').focus();}});

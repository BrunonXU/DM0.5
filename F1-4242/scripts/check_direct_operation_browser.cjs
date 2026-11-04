/* Isolated browser regression. Serves local assets and a fake API on a random
 * loopback port. No request is routed to a robot or a running workbench. */
const http = require('http'), fs = require('fs'), path = require('path');
const assert = require('assert'), cp = require('child_process'), os = require('os');
const WebSocket = require('ws');
const root = path.resolve(__dirname, '..'), output = process.env.F1_UIUX_BROWSER_OUTPUT || path.join(root, 'results/deployments/20261022-direct-operation/browser');
const profile = JSON.parse(fs.readFileSync(path.join(root, 'configs/f1-09.json')));
const groups = {left_arm:[],right_arm:[],body:[],head:[]};
Object.keys(profile.joint_limits_deg).forEach(n => groups[n.startsWith('arm_l')?'left_arm':n.startsWith('arm_r')?'right_arm':n.startsWith('body')?'body':'head'].push(n));
const target = Object.assign({}, ...Object.keys(profile.joint_limits_deg).map(n => ({[n]:0})), {body_j3:-5,body_j4:-10,body_j5:-5});
const robot = {id:'F1-test',label:'模拟机器人',groups:groups,joint_limits_deg:profile.joint_limits_deg,capabilities:{joints:{available:true},gripper:{available:true},base:{available:true}}};
let authenticated=true, lease=null, current=null, posts=[], mode='ready', serial=0;
const plan = {motion_s:8.03,estimated_total_s:8.18,parts:[{label:'左臂',status:'moving'},{label:'右臂',status:'moving'},{label:'身体',status:'moving'},{label:'头部',status:'moving'}],checkpoint_count:2};
let feedback=Object.assign({},target,{head_j2:0}),latest=null,otherOwner=false,reviewRequests=0,waistInFlight=0,waistDelay=1500;
let hilFixture={active:false,phase:'idle',run_id:null,status:{},events:[],summary_fresh:false},teleopFixture={phase:'idle',available:true},recorderFixture={mode:'idle',available:true,ready:true,frames:0,elapsed_s:0};
const hilRoutes=[['play','自由接入','teleop_play'],['demonstration','人工示教','demonstration'],['policy','策略 HIL','policy_hil']].map(([id,title,purpose])=>({id,title,purpose,ready:true,commands:['运行端','隧道','手柄'].map((where,i)=>({id:'step'+i,where,command:'echo fixture',hint:''}))}));
let startupFixture={stage:'idle',can_start:true,script_verified:true,script:'/fixture/start_all.sh',ec_script:'/fixture/start_ec_master.sh',existing_pids:[]},startupFailure=false,camerasFresh=false,cameraReads=0;
let planDelay=0, appScriptDelay=0;const reads=[];
let boot='mock-boot-1',ready=true,sourceActive=false,capabilityReason=null,connected=true,poseIssue=null;const reviewSpecs=[];
function state(){return {boot_id:boot,connected:connected,authenticated:authenticated,command_activity:{ready:ready,error:ready?null:'监听尚未就绪',active:{joint_command:sourceActive}},control:{lease_id:otherOwner?null:lease,owner:otherOwner?'其他浏览器':lease?'browser':null},action:latest,joints:Object.assign({},...Object.keys(target).map(n=>({[n]:{position:feedback[n],age_s:.01}}))),grippers:{left:{position:20,age_s:.01},right:{position:20,age_s:.01}},cameras:camerasFresh?{head:{age_s:.1},left:{age_s:.1},right:{age_s:.1}}:{},services:{},capabilities:capabilityReason?{joints:{available:false,reason:capabilityReason}}:robot.capabilities};}
const server = http.createServer(async (req,res)=>{
 const url = new URL(req.url,'http://127.0.0.1');if(req.method==='GET')reads.push(url.pathname);if(url.pathname==='/app.js'&&appScriptDelay)await new Promise(r=>setTimeout(r,appScriptDelay));let raw='';for await(const chunk of req)raw+=chunk;
 const body=raw?JSON.parse(raw):null;if(req.method==='POST')posts.push({path:url.pathname,body:body});
 let data;
 if(url.pathname==='/api/robot')data={robot:robot};
 else if(url.pathname==='/api/session')data={authenticated:authenticated};
 else if(url.pathname==='/api/state')data=state();
 else if(url.pathname==='/api/hil/state')data={hil:hilFixture};
 else if(url.pathname==='/api/hil/routes')data={routes:hilRoutes};
 else if(url.pathname==='/api/hil/interventions')data={jobs:[]};
 else if(url.pathname==='/api/teleop/state')data={teleop:{...teleopFixture,status_age_s:0,status_ttl_s:4,checked_unix:Date.now()/1000}};
 else if(url.pathname==='/api/recording/state')data={recording:recorderFixture};
 else if(url.pathname==='/api/hil/finish'){await new Promise(r=>setTimeout(r,200));data={run_id:'process-1'};}
 else if(url.pathname==='/api/data')data={available:true,episodes:Array.from({length:137},(_,i)=>({id:i.toString(16).padStart(24,'0'),name:'f1_hil_20261013T140457Z_'+String(i).padStart(8,'0'),path:'/data/hil_data/demonstration/'+i,source:'hil_demonstration',source_signature:'fixture',started_at:'2026-10-13T14:04:57Z',size_bytes:479200000,file_count:10,recording_status:'committed',review:{status:'pending',revision:'none'}})),summary:{episode_count:137}};
 else if(url.pathname==='/api/system')data={system:{version:'test'}};
 else if(url.pathname==='/api/presets')data={presets:[{id:'reset',name:'全身零位 · 原生 pose_2',source:'native_pose_2',joints:target},{id:'arms',name:'双臂复位',source:'lxl_xy_reset',joints:Object.assign({},...groups.left_arm.concat(groups.right_arm).map(n=>({[n]:target[n]})))},{id:'ready',name:'准备姿态',joints:target},{id:'custom',name:'自定义姿态',joints:target}]};
 else if(url.pathname==='/api/pose/review'){
  reviewRequests++;reviewSpecs.push(JSON.parse(url.searchParams.get('spec')));
  const error=!connected?'等待机器人连接':!ready?'监听尚未就绪':sourceActive?'其他控制源正在发送命令':capabilityReason;
  data={review:error?{executable:false,issues:[{message:error}]}:poseIssue?{executable:false,issues:[poseIssue]}:feedback.head_j2 < -25?{executable:false,issues:[{code:'origin_out_of_range',message:'head_j2 实测 -27.64°，超出配置范围 -25° ~ 25°。'}]}:{executable:true,plan:plan,issues:[]}};
 }
 else if(url.pathname==='/api/waist/preview'){
  const delta=Number(url.searchParams.get('delta_m')),delay=waistDelay;waistInFlight++;await new Promise(resolve=>setTimeout(resolve,delay));waistInFlight--;
  data={plan:{motion_s:2,targets:{body_j3:delta>0?-8:-12,body_j4:-14,body_j5:-6}}};
 }
 else if(url.pathname==='/api/actions/plan'){await wait(planDelay);data={plan:plan};}
 else if(url.pathname==='/api/control/acquire'){authenticated=true;lease='mock-browser-lease';data={authenticated:true,lease_id:lease};}
 else if(url.pathname==='/api/control/release'){lease=null;data={ok:true};}
 else if(url.pathname==='/api/control/heartbeat')data={ok:true};
 else if(url.pathname==='/api/actions'){current={id:'mock-'+(++serial),status:'accepted',progress:0};latest=current;data={action:current};}
 else if(url.pathname.startsWith('/api/actions/'))data={action:current};
 else if(url.pathname==='/api/stop'){current=Object.assign({},current,{status:'stopped',result:{commands_sent:true}});latest=current;data={action:current};}
 else if(url.pathname==='/api/dev/revision')data={revision:'test'};
 else if(url.pathname==='/api/events')data={events:[]};
 else if(url.pathname==='/api/startup/state'){if(startupFailure){res.writeHead(503,{'Content-Type':'application/json'});res.end(JSON.stringify({error:'fixture offline'}));return;}data={startup:startupFixture};}
 else if(url.pathname==='/api/startup/start'){startupFixture={...startupFixture,stage:'starting',can_start:false};data={startup:{accepted:true,message:'启动请求已受理'}};}
 else if(url.pathname==='/api/login'){authenticated=true;data={authenticated:true};}
 else if(url.pathname==='/api/logout'){authenticated=false;data={ok:true};}
 else if(url.pathname==='/api/teleop/prepare'){teleopFixture={id:'fixture-session',phase:'starting',variant:body.variant,available:true,can_confirm:false,can_stop:true,nonce:'fixture-nonce',status_age_s:0,status_ttl_s:4,checked_unix:Date.now()/1000};data={teleop:teleopFixture};}
 else if(url.pathname==='/api/teleop/confirm'){assert.strictEqual(body.nonce,'fixture-nonce');assert.strictEqual(body.session_id,'fixture-session');teleopFixture={...teleopFixture,phase:'running',can_confirm:false};data={teleop:teleopFixture};}
 else if(url.pathname==='/api/teleop/stop'){teleopFixture={...teleopFixture,phase:'stopped',can_stop:false};data={teleop:teleopFixture};}
 else if(url.pathname==='/api/recording/start'){recorderFixture={...recorderFixture,mode:'recording',frames:120,elapsed_s:4};data={recording:recorderFixture};}
 else if(url.pathname==='/api/recording/stop'){recorderFixture={...recorderFixture,mode:'review',review:{frames:120,duration_s:4,raw_save_allowed:true}};data={recording:recorderFixture};}
 else if(url.pathname==='/api/recording/save'){recorderFixture={...recorderFixture,mode:'idle',review:null,last_saved:{kind:'raw',data_episode_id:'aaaaaaaaaaaaaaaaaaaaaaaa',path:'/fixture/recording'}};data={recording:recorderFixture};}
 else if(url.pathname.startsWith('/api/cameras/')){cameraReads++;const key=url.pathname.split('/').pop().split('.')[0],file=path.join(root,'results/hil-layout-20261015',key+'.jpg');res.setHeader('Content-Type','image/jpeg');res.setHeader('X-F1-Frame-Id',String(cameraReads));res.setHeader('X-F1-Frame-Age-Ms','10');res.end(fs.readFileSync(file));return;}
 else if(url.pathname.startsWith('/api/'))data={};
 if(data!==undefined){res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));return;}
 if(process.env.F1_MOCK_MODEL&&url.pathname==='/model-viewer.js'){res.setHeader('Content-Type','text/javascript');res.end('export function createRobotViewer(){return {update(){},dispose(){},resize(){}}}');return;}
 const rel=url.pathname==='/'?'index.html':decodeURIComponent(url.pathname).slice(1),base=path.resolve(process.env.F1_UIUX_WEB_ROOT||path.join(root,'web'));let file=path.resolve(base,rel);if(!file.startsWith(base+path.sep)){res.statusCode=403;res.end();return;}if(!fs.existsSync(file))file=path.resolve(root,'web',rel);
 if(!fs.existsSync(file)){res.statusCode=404;res.end();return;}
 res.setHeader('Content-Type',({'.html':'text/html','.js':'text/javascript','.css':'text/css','.json':'application/json','.urdf':'text/xml','.stl':'application/octet-stream'})[path.extname(file)]||'application/octet-stream');fs.createReadStream(file).pipe(res);
});
const wait=ms=>new Promise(r=>setTimeout(r,ms));
function get(url){return new Promise((resolve,reject)=>http.get(url,r=>{let body='';r.on('data',b=>body+=b);r.on('end',()=>{try{resolve(JSON.parse(body))}catch(e){reject(e)}})}).on('error',reject));}
(async()=>{
 fs.mkdirSync(output,{recursive:true});await new Promise(r=>server.listen(0,'127.0.0.1',r));const port=server.address().port;
 const profileDir=fs.mkdtempSync(path.join(os.tmpdir(),'f1-pose-browser-'));
 const chrome=cp.spawn(process.env.CHROME_BIN||'/usr/bin/google-chrome',['--headless=new','--no-sandbox','--disable-dev-shm-usage','--enable-unsafe-swiftshader','--remote-debugging-port=0','--user-data-dir='+profileDir,'about:blank'],{stdio:'ignore'});
 let ws;
 try{
  const active=path.join(profileDir,'DevToolsActivePort');for(let i=0;i<100&&!fs.existsSync(active);i++)await wait(100);
  const debugPort=fs.readFileSync(active,'utf8').split('\n')[0],tabs=await get('http://127.0.0.1:'+debugPort+'/json/list');
  ws=new WebSocket(tabs.find(t=>t.type==='page').webSocketDebuggerUrl);await new Promise((r,j)=>{ws.on('open',r);ws.on('error',j)});
  let id=0;const pending=new Map(),exceptions=[];
  ws.on('message',raw=>{const p=JSON.parse(raw);if(p.id&&pending.has(p.id)){const h=pending.get(p.id);pending.delete(p.id);p.error?h.reject(Error(JSON.stringify(p.error))):h.resolve(p.result);}else if(p.method==='Runtime.exceptionThrown')exceptions.push(p.params.exceptionDetails);});
  const call=(method,params={})=>new Promise((resolve,reject)=>{const n=++id;pending.set(n,{resolve,reject});ws.send(JSON.stringify({id:n,method:method,params:params}));});
  const evaluate=async expression=>{const r=await call('Runtime.evaluate',{expression:expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
  const until=async(expression,label)=>{for(let i=0;i<80;i++){if(await evaluate(expression))return;await wait(100);}throw Error('Timed out: '+label);};
  const click=selector=>evaluate('document.querySelector('+JSON.stringify(selector)+').click()');
  const snap=async name=>{await evaluate("document.querySelector('#toast-region').replaceChildren()");const r=await call('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(output,name+'.png'),Buffer.from(r.data,'base64'));};

  await call('Runtime.enable');await call('Page.enable');

  const checks=[];
  const navigate=async hash=>{await call('Page.navigate',{url:'http://127.0.0.1:'+port+'/?test='+Date.now()+'#'+hash});await until("document.querySelector('#entry-notice')?.open",'entry notice');await until("document.querySelectorAll('#navigation .nav-item').length===4",'navigation');await wait(300);};
  // The entry UI must work even while the main module is still downloading.
  appScriptDelay=2500;
  await call('Page.navigate',{url:'http://127.0.0.1:'+port+'/?slow-module#overview'});
  await until("document.querySelector('#entry-notice')?.open",'notice before main module');
  assert.strictEqual(await evaluate("document.querySelectorAll('#navigation .nav-item').length"),0);
  await click('#enter-workbench');
  assert(!await evaluate("document.querySelector('#entry-notice').open"));
  await until("document.querySelectorAll('#navigation .nav-item').length===4",'workbench after delayed module');
  assert(!await evaluate("document.querySelector('#entry-notice').open"),'main module must not reopen dismissed notice');
  assert.strictEqual(posts.length,0);appScriptDelay=0;
  checks.push('notice usable before delayed app module; early entry neither reopens nor writes');
  for(const [width,height] of [[1920,1080],[1440,900],[1280,800],[1440,650],[1280,600]]){
   await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
   await navigate('overview');
   assert.strictEqual(posts.length,0,'opening only reads');
   const pausedReads=cameraReads;await wait(900);assert.strictEqual(cameraReads,pausedReads,'no camera traffic behind entry notice');
   const geometry=await evaluate(`(()=>{const d=document.querySelector('#entry-notice'),r=d.getBoundingClientRect();return {top:r.top,bottom:r.bottom,left:r.left,right:r.right,scroll:d.scrollHeight,client:d.clientHeight,buttons:d.querySelectorAll('button').length,inputs:d.querySelectorAll('input').length};})()`);
   assert(geometry.top>=0&&geometry.bottom<=height&&geometry.left>=0&&geometry.right<=width);
   assert(geometry.scroll<=geometry.client,'all modal content fits');assert.strictEqual(geometry.buttons,1);assert.strictEqual(geometry.inputs,0);
   assert.strictEqual(await evaluate("document.querySelectorAll('input[type=password]').length"),0);
   await snap('entry-'+width);
   await call('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
   assert(await evaluate("document.querySelector('#entry-notice').open"),'Escape must not bypass the notice');
   await click('#enter-workbench');assert.strictEqual(posts.length,0,'enter does not start or acquire');
   await wait(300);assert(cameraReads>pausedReads,'camera resumes after entry');
   await evaluate("location.hash='control'");await wait(250);
   assert(!await evaluate("document.querySelector('#entry-notice').open"),'internal navigation does not reopen');
   checks.push(width+'x'+height+': complete modal, single button, no password, zero startup writes');
  }
  const beforeModel=reads.filter(p=>p==='/model-viewer.js').length;
  await navigate('control');await wait(300);
  assert.strictEqual(reads.filter(p=>p==='/model-viewer.js').length,beforeModel,'control deep link defers model until entry');
  await click('#enter-workbench');await wait(400);
  assert(reads.filter(p=>p==='/model-viewer.js').length>beforeModel,'model loads after entry');
  checks.push('control deep link defers model and resumes after entry');
  await navigate('system');
  assert.strictEqual(await evaluate("document.querySelectorAll('dialog[open]').length"),1,'entry stays above legacy system deep link');
  await click('#enter-workbench');await until("document.querySelector('#workbench-drawer')?.open",'deferred system details');
  await click('[data-click="close-workbench"]');await evaluate("location.hash='control'");await wait(300);
  checks.push('legacy system deep link shows entry before details');
  // A rejected origin must name the actual axis, not imply physical inability
  // to return. The real page must preserve the draft and make no motion writes.
  for(const [width,height] of [[1920,1080],[1440,900],[1280,800]]){
   await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
   feedback.head_j1=-134.1064453125;feedback.head_j2=-27.56555938720703;
   poseIssue={part:'head',code:'origin_out_of_range',joint:'head_j1',current:feedback.head_j1,limits:[-40,40],message:'head_j1 实测 -134.11°，超出配置范围 -40°～40°。驱动会截断越界命令，需先核对该关节的标定与原生恢复方式。'};
   await click('[data-preset-index="0"]');
   await until("document.querySelector('#pose-plan-summary').textContent.includes('头部 J1 实测 -134.11°')",'specific J1 origin rejection');
   const before=posts.length;
   await click('[data-execute-required]');await wait(400);
   const summary=await evaluate("document.querySelector('#pose-plan-summary').textContent");
   assert(summary.includes('超出命令范围 -40°～40°')&&summary.includes('整套动作未开始'));
   assert(!summary.includes('无法平滑复位'));
   assert.strictEqual(posts.length,before,'blocked execution cannot acquire or submit motion');
   assert.strictEqual(await evaluate("document.querySelector('[data-target-input=head_j1]').value"),'0.0','target preserved');
   assert(await evaluate("document.querySelector('.pose-diagnostics pre').textContent.includes('驱动会截断越界命令')"),'full reason preserved');
   await snap('head-origin-'+width);
   checks.push(width+'x'+height+': J1 measured origin and command range retained, full reason + draft preserved, zero writes');
  }
  feedback.head_j1=0;feedback.head_j2=0;feedback.head_j3=20;
  poseIssue={part:'head',code:'origin_out_of_range',joint:'head_j3',current:20,limits:[-15,15],message:'head_j3 实测 20.00°，超出配置范围 -15°～15°。驱动会截断越界命令，需先核对该关节的标定与原生恢复方式。'};
  await click('[data-preset-index="0"]');
  await until("document.querySelector('#pose-plan-summary').textContent.includes('头部 J3 实测 20.00°，超出命令范围 -15°～15°')",'independent J3 diagnosis');
  poseIssue={part:'head',code:'blocked',message:'头部反馈超过本次边界试用幅度；不能继续'};
  await click('[data-preset-index="0"]');
  await until("document.querySelector('#pose-plan-summary').textContent==='头部反馈超过本次边界试用幅度；不能继续'",'boundary contract reason preserved');
  checks.push('independent J3 error remains accurate; boundary recovery rejection is not rewritten');
  poseIssue=null;feedback.head_j3=0;
  // Primary motion click obtains one lease, plans once, submits once; no popup.
  await until("document.querySelector('[data-preset-index]')",'presets');
  await click('[data-preset-index="0"]');await until("!document.querySelector('[data-execute-required]').disabled",'pose ready');
  await evaluate("document.querySelector('[data-execute-required]').click();document.querySelector('[data-execute-required]').click()");
  await until("document.querySelector('[data-execute-required]').textContent.includes('执行中')",'accepted');
  assert.strictEqual(posts.filter(p=>p.path==='/api/control/acquire').length,1);
  assert.strictEqual(posts.filter(p=>p.path==='/api/actions').length,1);
  assert(!await evaluate("document.querySelector('dialog[open]')"));
  assert(posts.every(p=>!('control_code' in p.body)&&!('token' in p.body)));
  assert(await evaluate("document.body.classList.contains('control-mode')&&getComputedStyle(document.querySelector('.topbar')).backgroundColor==='rgb(166, 65, 54)'&&!document.querySelector('#pose-control-status').hidden"),'control red strip remains visible');await snap('pose-direct');checks.push('one motion from repeated click, automatic lease, no confirmation or credentials');
  await click('#stop-button');await wait(300);current=latest={...current,status:'cancelled'};
  await click('[data-click="release"]');await wait(500);
  // Stop during planning must suppress the later action submission.
  const prior=posts.filter(p=>p.path==='/api/actions').length;planDelay=1000;
  await click('[data-preset-index="0"]');await until("!document.querySelector('[data-execute-required]').disabled",'retry ready');
  await click('[data-execute-required]');await until("document.body.classList.contains('control-mode')",'lease reacquired');
  await click('#stop-button');await wait(1400);
  assert.strictEqual(posts.filter(p=>p.path==='/api/actions').length,prior,'stop must cancel pending plan');planDelay=0;
  await click('[data-click="release"]');await wait(400);checks.push('stop cancels in-flight planning without delayed motion');
  otherOwner=true;await wait(1000);assert(await evaluate("document.querySelector('[data-execute-required]').disabled"),'other browser blocks execution');otherOwner=false;
  const beforeReload=posts.filter(p=>p.path==='/api/actions').length;
  await call('Page.reload');await until("document.querySelector('#entry-notice')?.open",'reload notice');await wait(1000);
  assert.strictEqual(posts.filter(p=>p.path==='/api/actions').length,beforeReload,'refresh does not resubmit');
  await click('#enter-workbench');checks.push('other owner respected; reload reopens notice without action');
  await evaluate("location.hash='overview'");await until("document.querySelector('#base-start-button')&&!document.querySelector('#base-start-button').disabled",'startup ready');
  await click('#base-start-button');await wait(400);
  assert.strictEqual(posts.filter(p=>p.path==='/api/startup/start').length,1);assert(!await evaluate("document.querySelector('dialog[open]')"));
  await evaluate("location.hash='pico?view=connect'");await until("document.querySelector('[data-teleop=prepare]')&&!document.querySelector('[data-teleop=prepare]').disabled",'Pico ready');
  await click('[data-teleop="prepare"][data-variant="upper"]');await wait(400);
  assert.strictEqual(posts.filter(p=>p.path==='/api/teleop/prepare').length,1);assert.strictEqual(posts.filter(p=>p.path==='/api/teleop/confirm').length,0);
  assert(!await evaluate("document.querySelector('dialog[open]')"));checks.push('Start All and Pico submit on one click');
  // Exercise the real Replay component with isolated review/lease hooks.
  await evaluate(`(async()=>{
   const {MotionReplay}=await import('/motion-replay.js');
   const root=document.createElement('section');document.querySelector('#main').append(root);
   window.replayPosts=[];window.replayLease=null;window.replayAction=null;window.replayAlign=false;
   window.replayTest=new MotionReplay(root,{id:'replay-fixture',source_signature:'fixture',name:'fixture'},{
    state:()=>({control:{lease_id:window.replayLease},action:window.replayAction}),lease:()=>window.replayLease,
    acquire:async()=>{window.replayLease='replay-lease'},online:()=>true,executing:()=>false,
    accept:a=>{window.replayAction=a},refresh:async()=>{},pauseHistory:()=>{},run:f=>f(),release:async()=>{},stop:async()=>{},
    api:async(path,body)=>{
     if(path.startsWith('/api/replay/reports'))return {reports:[]};
     if(path.startsWith('/api/replay/review'))return {review:{review_id:'single-use',valid_for_s:10,executable:!window.replayAlign,can_align:window.replayAlign,duration_s:2,start_s:0,end_s:2,command_count:20,events:[]}};
     window.replayPosts.push({path,body});return {action:{id:'replay-action',status:'running',replay:{episode_id:'replay-fixture'}}};
    }
   });window.replayTest.open();
  })()`);
  await until("document.querySelector('[data-r-start-task]')&&!document.querySelector('[data-r-start-task]').disabled",'replay ready');
  await click('[data-r-start-task]');await wait(200);
  assert.deepStrictEqual(await evaluate("window.replayPosts.map(p=>p.path)"),['/api/replay/start']);assert(!await evaluate("document.querySelector('dialog[open]')"));
  await evaluate("window.replayAction=null;window.replayAlign=true;window.replayTest.update();window.replayTest.check()");
  await until("!document.querySelector('[data-r-align]').disabled",'alignment ready');await click('[data-r-align]');await wait(200);
  assert.deepStrictEqual(await evaluate("window.replayPosts.map(p=>p.path)"),['/api/replay/start','/api/replay/align']);assert(!await evaluate("document.querySelector('dialog[open]')"));
  checks.push('Replay and align use one click and keep review + lease binding');
  assert.strictEqual(exceptions.length,0,JSON.stringify(exceptions));
  const report={result:'passed',checks,exceptions,posts,source:'isolated loopback fake API; no robot requests'};
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({result:report.result,checks},null,2));
 }finally{if(ws)ws.close();chrome.kill('SIGTERM');server.closeAllConnections?.();server.close();}
})().catch(e=>{console.error(e);server.closeAllConnections?.();server.close();process.exitCode=1;});

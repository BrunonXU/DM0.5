/* Isolated browser regression. Serves local assets and a fake API on a random
 * loopback port. No request is routed to a robot or a running workbench. */
const http = require('http'), fs = require('fs'), path = require('path');
const assert = require('assert'), cp = require('child_process'), os = require('os');
const WebSocket = require('ws');
const root = path.resolve(__dirname, '..'), output = process.env.F1_UIUX_BROWSER_OUTPUT || path.join(root, 'results/workbench-unified-browser');
const profile = JSON.parse(fs.readFileSync(path.join(root, 'configs/f1-09.json')));
const groups = {left_arm:[],right_arm:[],body:[],head:[]};
Object.keys(profile.joint_limits_deg).forEach(n => groups[n.startsWith('arm_l')?'left_arm':n.startsWith('arm_r')?'right_arm':n.startsWith('body')?'body':'head'].push(n));
const target = Object.assign({}, ...Object.keys(profile.joint_limits_deg).map(n => ({[n]:0})), {body_j3:-5,body_j4:-10,body_j5:-5});
const robot = {id:'F1-test',label:'模拟机器人',groups:groups,joint_limits_deg:profile.joint_limits_deg,capabilities:{joints:{available:true},gripper:{available:true},base:{available:true}}};
let authenticated=false, lease=null, current=null, posts=[], mode='ready', serial=0;
const plan = {motion_s:8.03,estimated_total_s:8.18,parts:[{label:'左臂',status:'moving'},{label:'右臂',status:'moving'},{label:'身体',status:'moving'},{label:'头部',status:'moving'}],checkpoint_count:2};
let feedback=Object.assign({},target,{head_j2:-27.64}),latest=null,otherOwner=false,reviewRequests=0,waistInFlight=0,waistDelay=1500;
let hilFixture={active:false,phase:'idle',run_id:null,status:{},events:[],summary_fresh:false},teleopFixture={phase:'idle',available:true},recorderFixture={mode:'idle',available:true,ready:true,frames:0,elapsed_s:0};
const hilRoutes=[['play','自由接入','teleop_play'],['demonstration','人工示教','demonstration'],['policy','策略 HIL','policy_hil']].map(([id,title,purpose])=>({id,title,purpose,ready:true,commands:['运行端','隧道','手柄'].map((where,i)=>({id:'step'+i,where,command:'echo fixture',hint:''}))}));
let startupFixture={stage:'idle',can_start:true,script_verified:true,script:'/fixture/start_all.sh',ec_script:'/fixture/start_ec_master.sh',existing_pids:[]},startupFailure=false,camerasFresh=false,cameraReads=0;
let boot='mock-boot-1',ready=true,sourceActive=false,capabilityReason=null,connected=true;const reviewSpecs=[];
function state(){return {boot_id:boot,connected:connected,authenticated:authenticated,command_activity:{ready:ready,error:ready?null:'监听尚未就绪',active:{joint_command:sourceActive}},control:{lease_id:otherOwner?null:lease,owner:otherOwner?'其他浏览器':lease?'browser':null},action:latest,joints:Object.assign({},...Object.keys(target).map(n=>({[n]:{position:feedback[n],age_s:.01}}))),grippers:{left:{position:20,age_s:.01},right:{position:20,age_s:.01}},cameras:camerasFresh?{head:{age_s:.1},left:{age_s:.1},right:{age_s:.1}}:{},services:{},capabilities:capabilityReason?{joints:{available:false,reason:capabilityReason}}:robot.capabilities};}
const server = http.createServer(async (req,res)=>{
 const url = new URL(req.url,'http://127.0.0.1');let raw='';for await(const chunk of req)raw+=chunk;
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
  data={review:error?{executable:false,issues:[{message:error}]}:feedback.head_j2 < -25?{executable:false,issues:[{code:'origin_out_of_range',message:'head_j2 实测 -27.64°，超出配置范围 -25° ~ 25°。'}]}:{executable:true,plan:plan,issues:[]}};
 }
 else if(url.pathname==='/api/waist/preview'){
  const delta=Number(url.searchParams.get('delta_m')),delay=waistDelay;waistInFlight++;await new Promise(resolve=>setTimeout(resolve,delay));waistInFlight--;
  data={plan:{motion_s:2,targets:{body_j3:delta>0?-8:-12,body_j4:-14,body_j5:-6}}};
 }
 else if(url.pathname==='/api/actions/plan')data={plan:plan};
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
 else if(url.pathname==='/api/teleop/prepare'){teleopFixture={id:'fixture-session',phase:'awaiting_confirmation',variant:body.variant,available:true,can_confirm:true,can_stop:true,nonce:'fixture-nonce',status_age_s:0,status_ttl_s:4,checked_unix:Date.now()/1000};data={teleop:teleopFixture};}
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
  const navigate=async hash=>{await call('Page.navigate',{url:'http://127.0.0.1:'+port+'/#'+hash});await until("document.querySelectorAll('#navigation .nav-item').length===5",'five navigation entries');await until('document.querySelector("#main").children.length>0','page rendered');await wait(300);};
  await call('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false});
  await navigate('overview');await until("!document.querySelector('#base-start-button').disabled",'startup available');
  assert(!posts.length,'opening overview must not start anything');
  await snap('overview-idle');
  await click('#base-start-button');await until("document.querySelector('#login-dialog')?.open",'login required');
  await click('#login-cancel');assert(!posts.length,'cancel login is read only');
  authenticated=true;await wait(900);await click('#base-start-button');await until("document.querySelector('#startup-confirm-dialog')?.open",'startup confirmation');
  await snap('startup-confirm');
  await click('#startup-confirm-dialog button[type=submit]');assert(!posts.length,'checkbox required');
  await click('#startup-confirm-dialog [data-cancel]');assert(!posts.length,'cancel startup is read only');await until("!document.querySelector('#base-start-button').disabled",'startup restored after cancellation');
  await click('#base-start-button');await until("document.querySelector('#startup-confirm-dialog')?.open",'second startup confirmation');
  await click('#startup-confirm-dialog input[type=checkbox]');await click('#startup-confirm-dialog button[type=submit]');
  await until("document.querySelector('#startup-title').textContent.includes('启动中')",'accepted startup');
  assert.strictEqual(posts.filter(p=>p.path==='/api/startup/start').length,1);
  assert.deepStrictEqual(posts.find(p=>p.path==='/api/startup/start').body,{confirm:true});
  startupFixture={...startupFixture,stage:'completed',can_start:false,result:'success',exit_status:'0',existing_pids:[123]};
  await click('[data-click=refresh]');await until("document.querySelector('#startup-title').textContent==='基础服务待检查'",'script completion is not readiness');
  camerasFresh=true;await wait(900);await until("document.querySelector('#startup-title').textContent==='基础服务已就绪'",'real feedback readiness');
  assert(await evaluate("document.querySelector('#base-start-button').hidden"));
  startupFailure=true;await click('[data-click=refresh]');await until("document.querySelector('#startup-title').textContent==='基础服务状态待确认'",'startup loss is explicit');
  assert(await evaluate("document.querySelector('#base-start-button').disabled"));startupFailure=false;
  await click('[data-click=refresh]');
  await click('[data-click=system-details]');await until("document.querySelector('#workbench-drawer').open",'system drawer');await snap('system-identity');
  await evaluate("window.savedClipboard=navigator.clipboard;Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:()=>Promise.reject(Error('fixture denied'))}});window.savedExec=document.execCommand;window.copyInsideDialog=false;document.execCommand=()=>{window.copyInsideDialog=!!document.activeElement.closest('#workbench-drawer');return true}");
  await click('[data-click=copy-workbench]');await wait(50);assert(await evaluate('copyInsideDialog'));await evaluate("document.execCommand=savedExec;Object.defineProperty(navigator,'clipboard',{configurable:true,value:savedClipboard})");
  await click('[data-system-tab=connection]');assert(await evaluate("document.querySelector('#revision-label').textContent==='test'"));await snap('system-connection');
  await click('#workbench-drawer [data-click=startup-details]');await until("document.querySelector('#startup-result')?.textContent.includes('脚本已完成')",'startup details');
  await evaluate("document.querySelector('.workbench-disclosure').open=true");await snap('startup-details');
  await click('[data-click=close-workbench]');await click('.topbar [data-click=session-details]');await snap('session');
  assert(await evaluate("!document.querySelector('#workbench-drawer [data-click=logout]').hidden"));await click('[data-click=close-workbench]');
  await navigate('system');await until("location.hash==='#overview'&&document.querySelector('#workbench-drawer')?.open",'legacy system route retained');await click('[data-click=close-workbench]');
  checks.push('Startup authenticated and explicitly confirmed; cancel sends nothing; one POST; completed script does not imply readiness; disconnected startup blocks requests; all system functions accessible; legacy #system redirects');

  await navigate('pico');await until("document.querySelector('#pico-tab-live')?.getAttribute('aria-selected')==='true'",'pico live');
  await until("document.querySelector('[data-camera=head] img').naturalWidth>0",'camera live');
  await click('[data-pico-view=connect]');await wait(400);let count=cameraReads;await wait(900);assert.strictEqual(cameraReads,count,'hidden cameras pause');
  await until("!document.querySelector('[data-teleop=prepare][data-variant=upper]').disabled",'upper prepare');
  await click('[data-teleop=prepare][data-variant=upper]');await until("document.querySelector('[data-teleop=confirm]')&&!document.querySelector('[data-teleop=confirm]').disabled",'teleop confirmation visible');await snap('pico-confirm');
  assert.strictEqual(posts.filter(p=>p.path==='/api/teleop/confirm').length,0,'prepare must not confirm');
  await click('[data-teleop=confirm]');await until("document.querySelector('#teleop-session').textContent.includes('遥操进程就绪')",'running teleop');
  assert.strictEqual(posts.filter(p=>p.path==='/api/teleop/confirm').length,1);
  await click('[data-pico-view=live]');await until('document.querySelector("#pico-view-live").hidden===false','live selected');await wait(900);assert(cameraReads>count,'camera resumes');
  await click('#pico-tab-live');assert(await evaluate("document.querySelector('#pico-persistent-stop').hidden===false"));
  await click('[data-pico-view=record]');await until("!document.querySelector('[data-recording=start]').disabled",'recording available');
  await click('[data-recording=start]');await until("document.querySelector('[data-recording=stop]')",'recording started');
  await click('[data-pico-view=help]');await wait(300);assert.strictEqual(posts.filter(p=>p.path==='/api/recording/stop').length,0,'tab change does not stop recording');
  await click('[data-pico-view=record]');await click('[data-recording=stop]');await until("document.querySelector('[data-recording=save]')",'save shown');
  await snap('pico-record-review');await click('[data-pico-view=help]');await click('[data-pico-view=record]');assert(await evaluate("!!document.querySelector('[data-recording=save]')"),'pending save survives tabs');
  await click('[data-recording=save]');await until("document.querySelector('.recording-last-saved a')",'saved record link');
  assert.strictEqual(posts.filter(p=>p.path==='/api/recording/save').length,1);
  await click('#pico-persistent-stop');await until("document.querySelector('#pico-persistent-stop').hidden",'teleop stop');
  await evaluate("document.querySelector('#pico-tab-record').dispatchEvent(new KeyboardEvent('keydown',{key:'End',bubbles:true}))");
  assert(await evaluate("document.activeElement.id==='pico-tab-help'&&document.querySelector('#pico-tab-help').getAttribute('aria-selected')==='true'"));
  checks.push('Pico horizontal tabs: prepare/nonce confirmation/stop preserved; record/start/stop/save preserved across tabs; hidden cameras pause and resume; keyboard tabs work');

  for(const [width,height] of [[1440,900],[1280,800],[1920,1080]]){
   await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});let geometry=null;
   for(const route of ['overview','control','pico','pico?view=connect','pico?view=record','pico?view=help','hil','hil?route=demonstration','data']){
    await navigate(route);if(route.startsWith('hil'))await until("document.querySelector('.hil-horizontal')",'hil layout');
    await until("document.body.dataset.page==='"+route.split('?')[0]+"'",'correct page');
    const g=await evaluate("(()=>{const e=document.querySelector('.sidebar'),n=document.querySelector('.nav-item'),b=document.querySelector('.infra-brand');return [e.getBoundingClientRect().width,n.getBoundingClientRect().height,b.getBoundingClientRect().y,getComputedStyle(n).fontSize,document.querySelector('.nav-toggle').getBoundingClientRect().y]})()");
    if(geometry)assert.deepStrictEqual(g,geometry,'sidebar must not jump '+route);else geometry=g;
    assert(await evaluate('document.documentElement.scrollWidth<=innerWidth'),'overflow '+route+' '+width);
    if(route==='overview'){assert(await evaluate("document.querySelector('.workbench-entries').getBoundingClientRect().bottom<innerHeight"));assert(await evaluate("getComputedStyle(document.querySelector('.device-dot')).width==='10px'"));}
    await snap(route.replace(/[?=]/g,'-')+'-'+width);
   }
   await click('[data-click=toggle-nav]');assert(await evaluate("document.querySelector('.sidebar').getBoundingClientRect().width===76"));await click('[data-click=toggle-nav]');
   checks.push(width+'x'+height+': all routes visible, consistent navigation geometry, 10px device dots, no overflow');
  }
  assert.strictEqual(exceptions.length,0,JSON.stringify(exceptions));
  const permitted=['/api/startup/start','/api/teleop/prepare','/api/teleop/confirm','/api/teleop/stop','/api/recording/start','/api/recording/stop','/api/recording/save'];
  assert(posts.every(p=>permitted.includes(p.path)),'unexpected write');
  const report={result:'passed',checks,exceptions,posts,source:'isolated loopback fake API; no robot requests'};
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
 }finally{if(ws)ws.close();chrome.kill('SIGTERM');server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});


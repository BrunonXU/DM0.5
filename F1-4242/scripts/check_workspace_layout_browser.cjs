/* Isolated browser regression. Serves local assets and a fake API on a random
 * loopback port. No request is routed to a robot or a running workbench. */
const http = require('http'), fs = require('fs'), path = require('path');
const assert = require('assert'), cp = require('child_process'), os = require('os');
const WebSocket = require('ws');
const root = path.resolve(__dirname, '..'), output = process.env.F1_UIUX_BROWSER_OUTPUT || path.join(root, 'results/workspace-redesign-20261029/browser');
const profile = JSON.parse(fs.readFileSync(path.join(root, 'configs/f1-09.json')));
const groups = {left_arm:[],right_arm:[],body:[],head:[]};
Object.keys(profile.joint_limits_deg).forEach(n => groups[n.startsWith('arm_l')?'left_arm':n.startsWith('arm_r')?'right_arm':n.startsWith('body')?'body':'head'].push(n));
const target = Object.assign({}, ...Object.keys(profile.joint_limits_deg).map(n => ({[n]:0})), {body_j3:-5,body_j4:-10,body_j5:-5});
const robot = {id:'F1-09',label:'模拟机器人',groups:groups,joint_limits_deg:profile.joint_limits_deg,capabilities:{joints:{available:true},gripper:{available:true},base:{available:true}}};
let authenticated=true, lease=null, current=null, posts=[], mode='ready', serial=0;
const plan = {motion_s:8.03,estimated_total_s:8.18,parts:[{label:'左臂',status:'moving'},{label:'右臂',status:'moving'},{label:'身体',status:'moving'},{label:'头部',status:'moving'}],checkpoint_count:2};
let feedback=Object.assign({},target,{head_j2:0}),latest=null,otherOwner=false,reviewRequests=0,waistInFlight=0,waistDelay=1500;
let hilFixture={active:false,phase:'idle',status:{},events:[],summary_fresh:false,next_step:'展开启动指引，按顺序打开三个终端。',completion:{phase:'saved',run_id:'previous-episode'}},teleopFixture={phase:'idle',available:true},recorderFixture={mode:'idle',available:true,ready:true,frames:0,elapsed_s:0};
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
 else if(url.pathname==='/api/events')data={events:Array.from({length:40},(_,i)=>({at:Date.now()-i*1000,type:'action_result',message:'模拟操作结果：任务已结束'}))};
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
  const goto=async hash=>{await evaluate('location.hash='+JSON.stringify(hash));await wait(1200);};
  camerasFresh=true;
  await call('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false});
  await call('Page.navigate',{url:'http://127.0.0.1:'+port+'/#overview'});
  await until("document.querySelectorAll('#navigation .nav-item').length===4",'four navigation entries');
  await until("document.querySelector('#entry-notice')?.open",'entry notice');await click('#enter-workbench');await wait(1500);
  assert.strictEqual(posts.length,0);
  assert(await evaluate("document.querySelector('.workspace-help').href==='http://192.0.2.1:4243/?doc=f1-home'"));
  assert(await evaluate("!document.querySelector('#base-start-button').hidden && document.querySelector('#base-start-button').disabled"),'ready Start All remains visible');
  for(const [width,height] of [[1920,1080],[1440,900],[1280,800]]){
   await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
   for(const [name,hash] of [['overview','overview'],['debug','control'],['hil','hil?route=demonstration&view=live'],['single-window','hil?route=policy&model=rlt400&view=connect'],['pico-setup','pico?view=connect'],['pico-live','pico?view=live'],['data','data']]){
    await goto(hash);await evaluate('window.scrollTo(0,0)');
    assert(await evaluate('document.documentElement.scrollWidth<=innerWidth'),name+' fits desktop width '+width);
    if(name==='overview'){
     for(const collapsed of [false,true]){
      if(await evaluate("document.body.classList.contains('nav-collapsed')")!==collapsed)await click('#toggle-navigation');
      assert(await evaluate("(()=>{const m=document.querySelector('.workspace>main'),w=document.querySelector('.workspace');return Math.abs(m.getBoundingClientRect().width-Math.min(1840,w.getBoundingClientRect().width))<2&&getComputedStyle(document.querySelector('.page-heading h1')).fontSize==='28px'&&parseFloat(getComputedStyle(document.querySelector('.health-status')).fontSize)>=14&&parseInt(getComputedStyle(document.querySelector('.health-status')).fontWeight)>=600})()"),'overview shares page width and readable bold device state');
     }
     await click('#toggle-navigation');
     await until("[...document.querySelectorAll('[data-camera] img')].every(i=>i.naturalWidth>0)",'overview camera frames');
     const geometry=await evaluate(`(()=>{const b=s=>document.querySelector(s).getBoundingClientRect(),events=document.querySelector('.workbench-events');return {page:document.documentElement.scrollHeight,height:innerHeight,events:b('.workbench-events').bottom,camera:b('[data-camera-preview]').bottom,start:b('#base-start-button').height,nested:events.previousElementSibling?.classList.contains('workbench-devices'),shortcuts:document.querySelectorAll('.workbench-entries').length}})()`);
     fs.writeFileSync(path.join(output,'overview-geometry-'+width+'.json'),JSON.stringify(geometry,null,2));
     assert(geometry.nested&&geometry.shortcuts===0,'recent operations below device state; redundant shortcuts removed');
     assert(geometry.start>=54,'Start All is prominent');
    }
    await snap(name+'-'+width);
   }
   checks.push(width+'x'+height+': all workspaces rendered without horizontal page overflow');
  }
  await goto('hil');
  assert(await evaluate("(()=>{const n=document.querySelector('#sidebar-context'),p=document.querySelector('[data-nav=capture]'),d=document.querySelector('[data-nav=data]');return n.parentElement.id==='navigation'&&p.nextElementSibling===n&&n.nextElementSibling===d&&!n.querySelector('.context-guide')&&parseFloat(getComputedStyle(p).fontSize)>=17})()"),'capture children nested between Capture and Data; larger primary labels; no recipe link');
  assert(await evaluate("document.querySelectorAll('#hil-route-menu .hil-route-card').length===4"),'four equal workflow entries including free and RLT');
  assert(await evaluate("(()=>{const r=[...document.querySelectorAll('#hil-route-menu .hil-route-card')].map(e=>e.getBoundingClientRect());return r.every(x=>Math.abs(x.width-r[0].width)<1&&Math.abs(x.height-r[0].height)<1&&Math.abs(x.top-r[0].top)<1)})()"),'same-size portrait cards in one row');
  await snap('capture-entry');
  await goto('pico?view=connect');const beforeVariant=posts.length;
  await click('[data-pico-variant=lxl]');
  assert(await evaluate("document.querySelector('[data-pico-variant-panel=upper]').hidden&&!document.querySelector('[data-pico-variant-panel=lxl]').hidden"));
  await click('[data-pico-variant=upper]');
  assert.strictEqual(posts.length,beforeVariant,'selecting Pico instructions never launches teleop');
  await goto('hil?route=policy&model=rlt400&view=connect');
  assert(await evaluate("document.querySelector('#hil-overview h2').textContent.includes('未运行')&&!document.querySelector('#hil-persistent-status').textContent.includes('三个终端')&&document.querySelector('[data-hil-request-finish]').textContent.includes('结束运行端')"),'idle without run id must not look like a finishing session');
  assert(await evaluate("getComputedStyle(document.querySelector('[data-context-hil][aria-current=page]')).backgroundColor!==getComputedStyle(document.querySelector('[data-nav=capture]')).backgroundColor"),'secondary selection differs from primary navigation');
  assert(await evaluate("document.querySelectorAll('.capture-routes a').length===4&&document.querySelector('.capture-routes a[aria-current=page]').textContent.includes('局部 RLT')"),'all four modes persist with selected state');
  await click('.capture-disclosure:last-child summary');
  assert(await evaluate("document.querySelectorAll('.capture-disclosure:last-child [data-hil-copy]').length===3"),'legacy commands remain available');
  assert(await evaluate("document.querySelector('#hil-commands pre').textContent.includes('start_f1_rlt.cmd')&&!document.querySelector('#hil-commands pre').textContent.includes('--model')"));
  await goto('hil?route=policy&model=stageb1250&view=connect');
  assert(await evaluate("document.querySelector('#hil-commands pre').textContent.includes('--model stageb1250')"));
  assert.strictEqual(posts.length,0,'navigation and copying guides never starts capture');
  checks.push('F1-09 RLT and DAgger one-window commands; navigation is read-only');
  hilFixture={active:true,phase:'hil_latched',summary_fresh:true,summary_age_s:0,source:'structured_snapshot',run_id:'rlt-fixture',can_finish:true,session_key:{pid:123,run_id:'rlt-fixture',start_ticks:'123'},status:{purpose:'policy_hil',execution:'POWERED',rlt_phase:'manual',input_fresh:true,side:'right',rb:false,camera_age_ms:{head:10,left_wrist:10,right_wrist:10},recording:{phase:'recording',completed:321}}};
  await goto('hil?route=policy&model=rlt400&view=live');await wait(2100);
  assert(await evaluate("document.querySelector('#hil-overview').textContent.includes('↑确认局部成功')"),'RLT live instructions use the actual runtime');
  await snap('rlt-live');
  await click('[data-context-hil=teaching]');await evaluate("document.querySelector('[data-lesson=dpad_up]').dispatchEvent(new MouseEvent('click',{bubbles:true}))");await wait(200);
  assert(await evaluate("document.querySelector('#hil-lesson-title').textContent==='↑ · 确认局部成功'"));
  await evaluate("document.querySelector('[data-lesson=dpad_right]').dispatchEvent(new MouseEvent('click',{bubbles:true}))");assert(await evaluate("document.querySelector('#hil-lesson-title').textContent==='→ · 进入局部 RLT'"));
  hilFixture={...hilFixture,summary_fresh:false,summary_age_s:30};await click('[data-context-hil=live]');await wait(2100);
  assert(await evaluate("document.querySelector('[data-hil-request-finish]').disabled&&document.querySelector('#hil-overview').textContent.includes('状态未知')"));
  checks.push('actual RLT runtime instructions, dedicated ↑/→ teaching, stale session cannot finish');
  await goto('data');await click('.storage-popover>summary');await snap('storage-open');
  assert(await evaluate("document.querySelector('.storage-popover').open&&document.querySelector('#sidebar-context #data-version-tree')"));
  await call('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
  assert(await evaluate("!document.querySelector('.storage-popover').open"));
  await click('#sidebar-context [data-folder]');assert(await evaluate("!document.querySelector('#data-library-view').hidden"));
  assert(await evaluate("(()=>{const n=document.querySelector('#sidebar-context'),p=document.querySelector('[data-nav=data]');return n.parentElement.id==='navigation'&&p.nextElementSibling===n&&n.classList.contains('data-subnav')&&n.getBoundingClientRect().top-p.getBoundingClientRect().bottom<=12})()"),'Data children unfold immediately below Data, same as Capture');
  assert(await evaluate("getComputedStyle(document.querySelector('#sidebar-context button.current')).backgroundColor==='rgb(232, 238, 243)'&&getComputedStyle(document.querySelector('#sidebar-context button.current')).backgroundColor!==getComputedStyle(document.querySelector('[data-nav=data]')).backgroundColor"),'Data uses the same subordinate blue-gray selection');
  await click('#data-library-view [data-browse-mode=time]');
  assert(await evaluate("!document.querySelector('#sidebar-context button.current')&&!document.querySelector('#sidebar-context [data-browse-mode=time]')"),'timeline remains in content area only; sidebar contains folders');
  await snap('data-nested-navigation');

  checks.push('directory sidebar and storage popover, Escape closes and returns focus');
  await goto('pico?view=live');
  assert(await evaluate("document.querySelector('[data-pico-recorder-slot=live] #recorder-card')!==null"));
  teleopFixture={phase:'running',available:true,can_stop:true,id:'fixture-session',variant:'upper'};
  await wait(1600);await click('[data-recording=start]');await wait(500);await click('[data-recording=stop]');await wait(600);
  assert(await evaluate("!document.querySelector('#pico-persistent-stop').disabled"),'teleop stop remains independent in review');
  assert(await evaluate("document.querySelector('[data-recording=save]').getBoundingClientRect().bottom<=innerHeight"),'Pico save action visible at 1280x800');await snap('pico-review');await click('[data-recording=save]');await wait(600);
  assert.strictEqual(posts.filter(p=>p.path==='/api/teleop/stop').length,0,'saving does not stop teleop');
  await click('#pico-persistent-stop');await wait(500);assert.strictEqual(posts.filter(p=>p.path==='/api/teleop/stop').length,1);
  checks.push('Pico recorder lives beside cameras; stop/review/save independent from teleop stop');
  assert.strictEqual(exceptions.length,0,JSON.stringify(exceptions));
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({result:'passed',checks,exceptions,posts,source:'isolated fake API, no robot requests'},null,2));
  console.log(JSON.stringify({result:'passed',checks},null,2));
 }finally{if(ws)ws.close();chrome.kill('SIGTERM');server.closeAllConnections?.();server.close();}
})().catch(e=>{console.error(e);server.closeAllConnections?.();server.close();process.exitCode=1;});

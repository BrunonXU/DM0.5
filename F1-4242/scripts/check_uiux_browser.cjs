/* Isolated browser regression. Serves local assets and a fake API on a random
 * loopback port. No request is routed to a robot or a running workbench. */
const http = require('http'), fs = require('fs'), path = require('path');
const assert = require('assert'), cp = require('child_process'), os = require('os');
const WebSocket = require('ws');
const root = path.resolve(__dirname, '..'), output = process.env.F1_UIUX_BROWSER_OUTPUT || path.join(root, 'results/uiux-upgrade/pages');
const profile = JSON.parse(fs.readFileSync(path.join(root, 'configs/f1-09.json')));
const groups = {left_arm:[],right_arm:[],body:[],head:[]};
Object.keys(profile.joint_limits_deg).forEach(n => groups[n.startsWith('arm_l')?'left_arm':n.startsWith('arm_r')?'right_arm':n.startsWith('body')?'body':'head'].push(n));
const target = Object.assign({}, ...Object.keys(profile.joint_limits_deg).map(n => ({[n]:0})), {body_j3:-5,body_j4:-10,body_j5:-5});
const robot = {id:'F1-test',label:'模拟机器人',groups:groups,joint_limits_deg:profile.joint_limits_deg,capabilities:{joints:{available:true},gripper:{available:true},base:{available:true}}};
let authenticated=false, lease=null, current=null, posts=[], mode='ready', serial=0;
const plan = {motion_s:8.03,estimated_total_s:8.18,parts:[{label:'左臂',status:'moving'},{label:'右臂',status:'moving'},{label:'身体',status:'moving'},{label:'头部',status:'moving'}],checkpoint_count:2};
let feedback=Object.assign({},target,{head_j2:-27.64}),latest=null,otherOwner=false,reviewRequests=0,waistInFlight=0,waistDelay=1500;
let hilFixture={active:false,phase:'idle',status:{},events:[],summary_fresh:false},teleopFixture={phase:'idle',available:true},recorderFixture={mode:'idle',available:true,ready:true,frames:0,elapsed_s:0};
const hilRoutes=[['play','自由接入','teleop_play'],['demonstration','人工示教','demonstration'],['policy','策略 HIL','policy_hil']].map(([id,title,purpose])=>({id,title,purpose,ready:true,commands:['运行端','隧道','手柄'].map((where,i)=>({id:'step'+i,where,command:'echo fixture',hint:''}))}));
let boot='mock-boot-1',ready=true,sourceActive=false,capabilityReason=null,connected=true;const reviewSpecs=[];
function state(){return {boot_id:boot,connected:connected,authenticated:authenticated,command_activity:{ready:ready,error:ready?null:'监听尚未就绪',active:{joint_command:sourceActive}},control:{lease_id:otherOwner?null:lease,owner:otherOwner?'其他浏览器':lease?'browser':null},action:latest,joints:Object.assign({},...Object.keys(target).map(n=>({[n]:{position:feedback[n],age_s:.01}}))),grippers:{left:{position:20,age_s:.01},right:{position:20,age_s:.01}},cameras:{},services:{},capabilities:capabilityReason?{joints:{available:false,reason:capabilityReason}}:robot.capabilities};}
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
 else if(url.pathname==='/api/teleop/state')data={teleop:teleopFixture};
 else if(url.pathname==='/api/recording/state')data={recording:recorderFixture};
 else if(url.pathname==='/api/hil/finish'){await new Promise(r=>setTimeout(r,200));data={run_id:'process-1'};}
 else if(url.pathname==='/api/data')data={available:true,episodes:Array.from({length:137},(_,i)=>({id:i.toString(16).padStart(24,'0'),name:'f1_hil_20261013T140457Z_'+String(i).padStart(8,'0'),path:'/data/hil_data/demonstration/'+i,source:'hil_demonstration',source_signature:'fixture',started_at:'2026-10-13T14:04:57Z',size_bytes:479200000,file_count:10,recording_status:'committed',review:{status:'pending',revision:'none'}})),summary:{episode_count:137}};
 else if(url.pathname==='/api/system')data={system:{version:'test'}};
 else if(url.pathname==='/api/presets')data={presets:[{id:'reset',name:'全身零位 · 原生 pose_2',source:'native_pose_2',joints:target},{id:'arms',name:'双臂复位',source:'lxl_xy_reset',joints:Object.assign({},...groups.left_arm.concat(groups.right_arm).map(n=>({[n]:target[n]})))}]};
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
  const snap=async name=>{const r=await call('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(output,name+'.png'),Buffer.from(r.data,'base64'));};

  await call('Runtime.enable');await call('Page.enable');
  if(process.env.F1_UIUX_CAPTURE_ONLY){
   await call('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false});
   for(const page of ['overview','control','data','hil','pico','system']){
    await call('Page.navigate',{url:'http://127.0.0.1:'+port+'/#'+page});
    await until("document.querySelector('#main h1')",page);await wait(page==='control'?3500:900);
    if(page==='control')await until("document.querySelector('#robot-model .f1-model-status')?.textContent==='实时姿态'",'model loaded');
    if(page==='data'){await until("document.querySelector('[data-folder=DAgger]')",'folders');await click('[data-folder=DAgger]');await click('[data-folder="DAgger/人工示教"]');await wait(200);}
    await snap(page+'-1440');
   }
   fs.writeFileSync(path.join(output,'capture.json'),JSON.stringify({source:process.env.F1_UIUX_WEB_ROOT||'current',dimensions:[1440,900],writes:posts,exceptions:exceptions},null,2));return;
  }
  const checks=[];
  const noOverflow=async()=>assert.strictEqual(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true,'no page overflow');
  for(const width of [1920,1440,1280,768,390]){
   const height=width===1440?900:width===1920?1080:width===1280?800:844;
   await call('Emulation.setDeviceMetricsOverride',{width:width,height:height,deviceScaleFactor:1,mobile:width<600});
   await call('Page.navigate',{url:'http://127.0.0.1:'+port+'/#overview'});
   await until("document.querySelector('#work-status .line-item')",'overview');await wait(150);
   assert.strictEqual(await evaluate("getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()"),'#f5f5f7');
   assert.strictEqual(await evaluate("document.querySelector('.overview-current').getBoundingClientRect().bottom<document.querySelector('[data-camera-preview]').getBoundingClientRect().top"),true);
   await noOverflow();await snap('overview-'+width);
   await click('[data-nav=system]');await until("document.querySelector('#system-identity dd')",'system');await noOverflow();await snap('system-'+width);
   await click('[data-nav=control]');await until("document.querySelector('#arms-section')",'pose');
   if(!process.env.F1_MOCK_MODEL){await until("document.querySelector('#robot-model .f1-model-status')?.textContent==='实时姿态'",'loaded actual model');assert.strictEqual(await evaluate("document.querySelector('#robot-model canvas').clientHeight>200"),true,'model canvas must be visibly tall');await wait(200);}
   if(width>=1280){assert.strictEqual(await evaluate("document.querySelector('#robot-model').getBoundingClientRect().top<innerHeight-180"),true);assert.strictEqual(await evaluate("document.querySelector('.pose-toolbar').getBoundingClientRect().bottom<innerHeight"),true);}
   await noOverflow();await snap('pose-'+width);
   await evaluate("window.modelBefore=document.querySelector('#robot-model canvas');window.cameraBefore=document.querySelector('[data-camera-preview]');document.querySelector('.pose-presets').open=true");
   await click('[data-preset-index="0"]');await wait(100);
   assert.strictEqual(await evaluate("document.querySelector('#robot-model canvas')===window.modelBefore&&document.querySelector('[data-camera-preview]')===window.cameraBefore"),true,'preset preserves model and camera');
   assert.strictEqual(await evaluate("document.querySelector('#arms-section [data-dirty-count]').textContent.includes('已修改')"),true);
   await click('[data-click=align]');
   await click('[data-nav=pico]');await until("document.querySelector('#pico-options')",'Pico');await wait(100);await noOverflow();await snap('pico-'+width);
   await click('[data-nav=hil]');await until("document.querySelectorAll('.hil-route-card').length===3",'HIL routes');await noOverflow();await snap('hil-'+width);
   checks.push(width+'px: original theme, overview order, pose controls/model in viewport, preset DOM persistence, Pico/HIL navigation, no overflow');
  }
  await call('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false});
  authenticated=true;
  hilFixture={active:true,phase:'hil_latched',source:'structured_snapshot',summary_age_s:0,status_ttl_s:2,summary_fresh:true,can_finish:true,run_id:'process-1',session_key:{run_id:'process-1',pid:1,start_ticks:'x'},events:[],status:{purpose:'demonstration',execution:'POWERED',rb:true,side:'right',input_fresh:true,recording:{phase:'recording',completed:100,last_saved:{phase:'saved',run_id:'take-1',data_episode_id:'e'.repeat(24)}}}};
  await evaluate("location.hash='hil?route=play'");await until("document.querySelector('.hil-session-mismatch')",'route mismatch');
  assert.strictEqual(await evaluate("document.querySelector('#hil-recording-session').textContent.includes('人工示教')&&document.querySelector('[data-hil-finish]').disabled"),true);
  assert.strictEqual(await evaluate("document.querySelector('.hil-last-saved a').hash"),'#data?episode='+'e'.repeat(24));
  await snap('hil-route-mismatch');
  await evaluate("location.hash='hil?route=demonstration'");await until("document.querySelector('[data-hil-finish]')&&!document.querySelector('[data-hil-finish]').disabled",'active demo');
  await snap('hil-active');
  await evaluate("document.querySelector('.hil-teaching').open=true;document.querySelector('[data-lesson=start]').dispatchEvent(new MouseEvent('click',{bubbles:true}))");
  assert.strictEqual(await evaluate("document.querySelector('#hil-lesson-description').textContent.includes('Y 复位会被取消')"),true);
  await evaluate("document.querySelector('.hil-teaching').open=false");
  hilFixture.status.purpose='policy_hil';hilFixture.status.model_id='fixture-policy';hilFixture.status.policy_health='validated';hilFixture.status.policy_stage='executing';hilFixture.summary_age_s=9;
  await wait(350);
  assert.strictEqual(await evaluate("!document.querySelector('#hil-overview').textContent.includes('响应正常')&&!document.querySelector('#hil-overview').textContent.includes('执行动作')&&document.querySelector('[data-hil-finish]').disabled"),true);
  await snap('hil-stale');
  hilFixture.status.purpose='demonstration';hilFixture.summary_age_s=0;
  await wait(350);const beforeFinish=posts.length;
  await evaluate("document.querySelector('[data-hil-finish]').click();document.querySelector('[data-hil-finish]').click()");await wait(500);
  assert.strictEqual(posts.slice(beforeFinish).filter(p=>p.path==='/api/hil/finish').length,1);
  teleopFixture={id:'t'.repeat(32),phase:'running',variant:'upper',available:true,status_age_s:0,status_ttl_s:4,can_stop:true};
  recorderFixture={mode:'idle',available:true,ready:true,frames:0,elapsed_s:0,last_saved:{kind:'capture',data_episode_id:'e'.repeat(24),path:'/fixture/take'}};
  await evaluate("location.hash='pico'");await until("document.querySelector('#pico-options').open===false",'Pico running hierarchy');
  assert.strictEqual(await evaluate("document.querySelector('#recorder-card').hidden"),false);await noOverflow();await snap('pico-active');
  teleopFixture.variant='lxl';await wait(1200);assert.strictEqual(await evaluate("document.querySelector('#recorder-card').hidden"),true);
  teleopFixture.status_age_s=8;await wait(1200);assert.strictEqual(await evaluate("document.querySelector('#teleop-session .pill').classList.contains('good')"),false);
  checks.push('HIL actual route, stale policy, single finish, shared START help, exact saved episode, Pico active variant and stale state');
  assert.deepStrictEqual(exceptions,[]);
  assert(posts.every(p=>p.path==='/api/hil/finish'),'only explicit synthetic HIL finish writes');
  const report={result:'passed',source:'isolated loopback fake API; actual local model assets; no robot connection',checks:checks,console_exceptions:exceptions,write_requests:posts};
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
 }finally{if(ws)ws.close();chrome.kill('SIGTERM');server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});

/* Isolated browser regression. Serves local assets and a fake API on a random
 * loopback port. No request is routed to a robot or a running workbench. */
const http = require('http'), fs = require('fs'), path = require('path');
const assert = require('assert'), cp = require('child_process'), os = require('os');
const WebSocket = require('ws');
const root = path.resolve(__dirname, '..'), output = process.env.F1_POSE_BROWSER_OUTPUT || path.join(root, 'results/pose_sync_browser');
const profile = JSON.parse(fs.readFileSync(path.join(root, 'configs/f1-09.json')));
const groups = {left_arm:[],right_arm:[],body:[],head:[]};
Object.keys(profile.joint_limits_deg).forEach(n => groups[n.startsWith('arm_l')?'left_arm':n.startsWith('arm_r')?'right_arm':n.startsWith('body')?'body':'head'].push(n));
const target = Object.assign({}, ...Object.keys(profile.joint_limits_deg).map(n => ({[n]:0})), {body_j3:-5,body_j4:-10,body_j5:-5});
const robot = {id:'F1-test',label:'模拟机器人',groups:groups,joint_limits_deg:profile.joint_limits_deg,capabilities:{joints:{available:true},gripper:{available:true},base:{available:true}}};
let authenticated=false, lease=null, current=null, posts=[], mode='ready', serial=0;
const plan = {motion_s:8.03,estimated_total_s:8.18,parts:[{label:'左臂',status:'moving'},{label:'右臂',status:'moving'},{label:'身体',status:'moving'},{label:'头部',status:'moving'}],checkpoint_count:2};
let feedback=Object.assign({},target,{head_j2:-27.64}),latest=null,otherOwner=false,reviewRequests=0,waistInFlight=0,waistDelay=1500;
let boot='mock-boot-1',ready=true,sourceActive=false,capabilityReason=null,connected=true;const reviewSpecs=[];
function state(){return {boot_id:boot,connected:connected,authenticated:authenticated,command_activity:{ready:ready,error:ready?null:'监听尚未就绪',active:{joint_command:sourceActive}},control:{lease_id:otherOwner?null:lease,owner:otherOwner?'其他浏览器':lease?'browser':null},action:latest,joints:Object.assign({},...Object.keys(target).map(n=>({[n]:{position:feedback[n],age_s:.01}}))),grippers:{left:{position:20,age_s:.01},right:{position:20,age_s:.01}},cameras:{},services:{},capabilities:capabilityReason?{joints:{available:false,reason:capabilityReason}}:robot.capabilities};}
const server = http.createServer(async (req,res)=>{
 const url = new URL(req.url,'http://127.0.0.1');let raw='';for await(const chunk of req)raw+=chunk;
 const body=raw?JSON.parse(raw):null;if(req.method==='POST')posts.push({path:url.pathname,body:body});
 let data;
 if(url.pathname==='/api/robot')data={robot:robot};
 else if(url.pathname==='/api/session')data={authenticated:authenticated};
 else if(url.pathname==='/api/state')data=state();
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
 if(url.pathname==='/model-viewer.js'){res.setHeader('Content-Type','text/javascript');res.end('export function createRobotViewer(){return {update(){},dispose(){},resize(){}}}');return;}
 const rel=url.pathname==='/'?'index.html':decodeURIComponent(url.pathname).slice(1),file=path.resolve(root,'web',rel);
 if(!file.startsWith(path.join(root,'web')+path.sep)||!fs.existsSync(file)){res.statusCode=404;res.end();return;}
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
  await call('Runtime.enable');await call('Page.enable');await call('Emulation.setDeviceMetricsOverride',{width:1680,height:1100,deviceScaleFactor:1,mobile:false});
  await call('Page.navigate',{url:'http://127.0.0.1:'+port+'/#control'});
  const checks=[];
  const summary=()=>evaluate("document.querySelector('#pose-plan-summary').textContent");
  const actionPosts=()=>posts.filter(p=>p.path==='/api/actions');
  const input=async(name,value)=>evaluate("(()=>{const el=document.querySelector('[data-target-input=\""+name+"\"]');el.value="+JSON.stringify(String(value))+";el.dispatchEvent(new Event('input',{bubbles:true}));})()");
  const waitForReview=async(before,label)=>{for(let i=0;i<80&&reviewRequests===before;i++)await wait(100);assert(reviewRequests>before,label);await wait(100);};
  await until("document.querySelector('[data-preset-index=\"0\"]')",'presets');
  await click('[data-preset-index="0"]');await until("document.querySelector('#pose-plan-summary').textContent.includes('头部当前状态')",'initial head diagnostic');
  const reviewsBeforeExternal=reviewRequests;
  feedback.head_j2=0;latest={id:'external-head-reset',status:'succeeded',progress:1,result:{feedback_verified:true,commands_sent:true},targets:{head_j2:0}};
  await until("document.querySelector('[data-joint-live=\"head_j2\"]').textContent==='0.0'",'external recovery feedback arrives');
  await until("document.querySelector('#pose-plan-summary').textContent.includes('8.2')",'old head diagnostic replaced automatically');
  assert(reviewRequests>reviewsBeforeExternal);assert.strictEqual(await evaluate("document.querySelector('[data-target-input=\"head_j2\"]').value"),'0');
  checks.push('external recovery automatically refreshes old diagnostic without modifying targets');await snap('review-updated');
  let count=reviewRequests;for(let i=0;i<12;i++){feedback.head_j2=i%2?.04:-.04;await wait(250);}assert.strictEqual(reviewRequests,count);feedback.head_j2=0;
  checks.push('stable feedback and sub-threshold jitter do not rerun geometry');
  ready=false;await until("document.querySelector('#pose-plan-summary').textContent.includes('监听尚未就绪')",'listener readiness diagnostic');
  ready=true;sourceActive=true;await until("document.querySelector('#pose-plan-summary').textContent.includes('其他控制源')",'source active diagnostic');
  sourceActive=false;await until("document.querySelector('#pose-plan-summary').textContent.includes('8.2')",'source quiet restores review');
  capabilityReason='动作合同需要核验';await until("document.querySelector('#pose-plan-summary').textContent.includes('动作合同需要核验')",'capability diagnostic');
  capabilityReason=null;await until("document.querySelector('#pose-plan-summary').textContent.includes('8.2')",'capability recovered');
  connected=false;await until("document.querySelector('#pose-plan-summary').textContent.includes('等待机器人连接')",'connection lost');
  connected=true;await until("document.querySelector('#pose-plan-summary').textContent.includes('8.2')",'connection recovered');
  checks.push('listener readiness, command activity, capability reason and connection transitions refresh review');
  await click('[data-click="execute-current"]');await until("document.querySelector('#execute-dialog').open",'explicit authorization');
  count=reviewRequests;feedback.head_j2=2;latest={id:'external-later-result',status:'succeeded'};await wait(2500);
  assert.strictEqual(reviewRequests,count);assert.strictEqual(await evaluate("document.querySelector('[data-target-input=\"head_j2\"]').value"),'0');
  await click('#cancel-execute');assert.strictEqual(actionPosts().length,0);
  await waitForReview(count,'refresh resumes after cancelled authorization');
  checks.push('authorization freezes targets/review; cancel sends no action and rechecks current state');
  await click('[data-click="execute-current"]');await until("document.querySelector('#execute-dialog').open",'reopen authorization');
  await evaluate("document.querySelector('#execute-code').value='mock-only';document.querySelector('#execute-form').requestSubmit()");
  await until("!document.querySelector('#execute-dialog').open&&document.body.classList.contains('control-mode')",'single complete submission');
  assert.strictEqual(actionPosts().length,1);assert.strictEqual(Object.keys(actionPosts()[0].body.targets).length,22);
  count=reviewRequests;current.status='running';feedback.head_j2=1;await wait(2200);assert.strictEqual(reviewRequests,count);
  await click('#stop-button');await until("document.querySelector('#pose-plan-summary').textContent.includes('已停止')",'cancel task');
  assert.strictEqual(await evaluate("document.querySelector('[data-target-input=\"head_j2\"]').value"),'0');
  await click('[data-click="execute-current"]');await until("document.querySelector('#pose-scope').textContent.includes('正在')",'explicit retry');
  assert.strictEqual(actionPosts().length,2);assert.strictEqual(posts.filter(p=>p.path==='/api/control/acquire').length,1);
  feedback=Object.assign({},target);current.status='succeeded';current.progress=1;current.result={commands_sent:true,feedback_verified:true};
  await until("document.querySelector('#pose-scope').textContent.includes('已到位')",'own verified completion');
  checks.push('whole preset submits all 22 axes once; stop retains draft; retry uses the same authorization');
  feedback.body_j3=-35;await until("document.querySelector('#pose-scope').textContent.includes('上次')",'completed task becomes history after feedback drifts');
  assert(!(await summary()).includes('所有目标已确认到位'));
  feedback.body_j3=target.body_j3;await until("document.querySelector('#pose-scope').textContent.includes('已到位')",'same target is still confirmed fresh');
  // A reboot clears the server action and invalidates the previous browser lease.
  boot='mock-boot-2';latest=null;authenticated=false;lease=null;
  await until("!document.body.classList.contains('control-mode')&&!document.querySelector('#pose-scope').textContent.includes('已到位')",'reboot clears old completion');
  assert((await summary()).includes('状态已重置'));checks.push('feedback drift and reboot/action-null do not leave stale reached-target success');await snap('reset-state');
  // A different browser is moving: preserve edits, but do no geometry work.
  otherOwner=true;latest={id:'external-new-pose',status:'running',progress:.3,targets:{body_j3:-35}};
  await until("document.querySelector('#pose-scope').textContent==='其他会话正在执行'",'foreign active action visible');
  await click('[data-preset-index="0"]');count=reviewRequests;
  for(let i=0;i<8;i++){feedback.body_j3=-20-i;await wait(300);}assert.strictEqual(reviewRequests,count);
  latest.status='succeeded';otherOwner=false;await waitForReview(count,'one recheck after foreign execution');
  await until("document.querySelector('#pose-plan-summary').textContent.includes('8.2')",'review recovered after foreign completion');
  checks.push('foreign running action is displayed and pauses automatic planning until completion');
  // Reload to isolate navigation and asynchronous height requests.
  authenticated=false;lease=null;otherOwner=false;current=null;latest=null;feedback=Object.assign({},target);
  await call('Page.reload');await until("document.querySelector('[data-preset-index=\"0\"]')",'fresh page');
  await click('[data-preset-index="0"]');await evaluate("document.querySelector('#body-section').open=true");
  await click('[data-waist-step="2"]');for(let i=0;i<30&&!waistInFlight;i++)await wait(50);assert.strictEqual(waistInFlight,1);
  await click('[data-group="base"]');await until("document.querySelector('[data-jog=\"forward\"]')",'move tab');
  for(let i=0;i<50&&waistInFlight;i++)await wait(50);assert.strictEqual(waistInFlight,0);await wait(100);
  await click('[data-group="pose"]');
  await until("document.querySelector('#pose-plan-summary').textContent.includes('8.2')",'waist review resumes after tab change');
  assert.strictEqual(await evaluate("document.querySelector('#waist-delta').value"),'2');
  assert.strictEqual(await evaluate("document.querySelector('[data-target-input=\"body_j3\"]').value"),'-8');
  await input('arm_l_j5',12);await input('body_j1',15);await input('head_j1',8);
  await click('[data-click="execute-current"]');await until("document.querySelector('#execute-dialog').open",'combined pose authorization');
  await evaluate("document.querySelector('#execute-code').value='mock-only';document.querySelector('#execute-form').requestSubmit()");
  await until("!document.querySelector('#execute-dialog').open&&document.querySelector('#pose-scope').textContent.includes('正在')",'combined task accepted');
  const combined=actionPosts()[actionPosts().length-1].body;
  assert.strictEqual(combined.waist_delta_m,.02);assert.strictEqual(combined.targets.arm_l_j5,12);assert.strictEqual(combined.targets.body_j1,15);assert.strictEqual(combined.targets.head_j1,8);
  ['body_j3','body_j4','body_j5'].forEach(n=>assert.strictEqual(combined.targets[n],undefined));
  assert.strictEqual(combined.head_boundary_recovery_ack,undefined);
  checks.push('waist survives move-tab roundtrip; arms/head/body rotation and height submit as one pose, without duplicate lift joint targets');
  await click('#stop-button');await until("document.querySelector('#pose-plan-summary').textContent.includes('已停止')",'stop combined task');
  // Older preview must not clear or overwrite a newer request.
  waistDelay=500;await click('[data-waist-step="2"]');for(let i=0;i<30&&!waistInFlight;i++)await wait(50);
  waistDelay=1400;await click('[data-waist-step="-2"]');await wait(850);
  assert.strictEqual(await evaluate("document.querySelector('#waist-plan-status').textContent.includes('正在计算')"),true);
  for(let i=0;i<50&&waistInFlight;i++)await wait(50);
  await until("document.querySelector('[data-target-input=\"body_j3\"]').value==='-12'",'latest waist response wins');
  assert.strictEqual(await evaluate("document.querySelector('#waist-delta').value"),'-2');
  // Changing the page must not dereference an absent status element.
  waistDelay=500;await click('[data-waist-step="2"]');await click('[data-nav="overview"]');
  await wait(1200);await click('[data-nav="control"]');
  await until("document.querySelector('[data-target-input=\"body_j3\"]').value==='-8'",'waist result survives page navigation');
  assert.strictEqual(await evaluate("document.querySelector('#waist-delta').value"),'2');
  // Re-alignment explicitly discards the pending result.
  waistDelay=900;await click('[data-waist-step="-2"]');for(let i=0;i<30&&!waistInFlight;i++)await wait(50);
  await click('[data-click="align"]');await wait(1200);
  assert.strictEqual(await evaluate("document.querySelector('#waist-delta').value"),'0');
  assert.strictEqual(await evaluate("document.querySelector('[data-target-input=\"body_j3\"]').value"),String(target.body_j3));
  checks.push('older preview cannot clear newer pending or overwrite edits; page navigation is safe; re-alignment discards old request');await snap('navigation-stable');
  assert.deepStrictEqual(exceptions,[]);
  const report={result:'passed',source:'actual local app.js with isolated fake API; model rendering stubbed; no robot or remote access',checks:checks,action_posts:actionPosts().length,authorization_posts:posts.filter(p=>p.path==='/api/control/acquire').length,review_requests:reviewRequests,console_exceptions:exceptions};
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));

 }finally{if(ws)ws.close();chrome.kill('SIGTERM');server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});

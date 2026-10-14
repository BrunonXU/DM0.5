/* Isolated browser regression. Serves local assets and a fake API on a random
 * loopback port. No request is routed to a robot or a running workbench. */
const http = require('http'), fs = require('fs'), path = require('path');
const assert = require('assert'), cp = require('child_process'), os = require('os');
const WebSocket = require('ws');
const root = path.resolve(__dirname, '..'), output = process.env.F1_POSE_BROWSER_OUTPUT || path.join(root, 'results/head_boundary_browser');
const profile = JSON.parse(fs.readFileSync(path.join(root, 'configs/f1-09.json')));
const groups = {left_arm:[],right_arm:[],body:[],head:[]};
Object.keys(profile.joint_limits_deg).forEach(n => groups[n.startsWith('arm_l')?'left_arm':n.startsWith('arm_r')?'right_arm':n.startsWith('body')?'body':'head'].push(n));
const target = Object.assign({}, ...Object.keys(profile.joint_limits_deg).map(n => ({[n]:0})), {body_j3:-5,body_j4:-10,body_j5:-5});
const robot = {id:'F1-test',label:'模拟机器人',groups:groups,joint_limits_deg:profile.joint_limits_deg,capabilities:{joints:{available:true},gripper:{available:true},base:{available:true}}};
let authenticated=false, lease=null, current=null, posts=[], mode='recovery', serial=0;
const plan = {motion_s:8.03,estimated_total_s:8.18,parts:[{label:'左臂',status:'moving'},{label:'右臂',status:'moving'},{label:'身体',status:'moving'},{label:'头部',status:'moving'}],checkpoint_count:2};
const recovery={joint:'head_j2',origin:-27.64,edge:-25,outside_deg:2.64,speed_controlled:false};
let headPosition=recovery.origin;
function selectedPlan(review){return mode==='ordinary'||(review&&mode==='late')?plan:Object.assign({},plan,{head_boundary_recovery:Object.assign({},recovery,mode==='invalid'?{speed_controlled:true}:{})});}
function state(){return {connected:true,authenticated:authenticated,control:{lease_id:lease,owner:lease?'browser':null},joints:Object.assign({},...Object.keys(target).map(n=>({[n]:{position:n==='head_j2'?headPosition:target[n],age_s:.01}}))),grippers:{left:{position:20,age_s:.01},right:{position:20,age_s:.01}},cameras:{},services:{},capabilities:robot.capabilities};}
const server = http.createServer(async (req,res)=>{
 const url = new URL(req.url,'http://127.0.0.1');let raw='';for await(const chunk of req)raw+=chunk;
 const body=raw?JSON.parse(raw):null;if(req.method==='POST')posts.push({path:url.pathname,body:body});
 let data;
 if(url.pathname==='/api/robot')data={robot:robot};
 else if(url.pathname==='/api/session')data={authenticated:authenticated};
 else if(url.pathname==='/api/state')data=state();
 else if(url.pathname==='/api/system')data={system:{version:'test'}};
 else if(url.pathname==='/api/presets')data={presets:[{id:'reset',name:'全身零位 · 原生 pose_2',source:'native_pose_2',joints:target},{id:'arms',name:'双臂复位',source:'lxl_xy_reset',joints:Object.assign({},...groups.left_arm.concat(groups.right_arm).map(n=>({[n]:target[n]})))}]};
 else if(url.pathname==='/api/pose/review')data={review:{executable:true,plan:selectedPlan(true),issues:[]}};
 else if(url.pathname==='/api/actions/plan')data={plan:selectedPlan(false)};
 else if(url.pathname==='/api/control/acquire'){authenticated=true;lease='mock-browser-lease';data={authenticated:true,lease_id:lease};}
 else if(url.pathname==='/api/control/release'){lease=null;data={ok:true};}
 else if(url.pathname==='/api/control/heartbeat')data={ok:true};
 else if(url.pathname==='/api/actions'){current={id:'mock-'+(++serial),status:'accepted',progress:0};data={action:current};}
 else if(url.pathname.startsWith('/api/actions/'))data={action:current};
 else if(url.pathname==='/api/stop'){current=Object.assign({},current,{status:'cancelled',result:{commands_sent:true,head_boundary_recovery:{boundary_command_sent:true,physical_stop_confirmed:false}}});data={action:current};}
 else if(url.pathname==='/api/dev/revision')data={revision:'test'};
 else if(url.pathname==='/api/events')data={events:[]};
 else if(url.pathname.startsWith('/api/'))data={};
 if(data!==undefined){res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));return;}
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
  const until=async(expression,label)=>{for(let i=0;i<80;i++){if(await evaluate(expression))return;await wait(100);}throw Error('Timed out: '+label+' '+JSON.stringify(await evaluate("({summary:document.querySelector('#pose-plan-summary')?.textContent,error:document.querySelector('#execute-error')?.textContent,dialog:document.querySelector('#execute-dialog')?.open,executeDisabled:document.querySelector('[data-click=\"execute-current\"]')?.disabled})")));};
  const click=selector=>evaluate('document.querySelector('+JSON.stringify(selector)+').click()');
  const snap=async name=>{const r=await call('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(output,name+'.png'),Buffer.from(r.data,'base64'));};
  await call('Runtime.enable');await call('Page.enable');await call('Emulation.setDeviceMetricsOverride',{width:1680,height:1100,deviceScaleFactor:1,mobile:false});
  await call('Page.navigate',{url:'http://127.0.0.1:'+port+'/#control'});
  const actionPosts=()=>posts.filter(p=>p.path==='/api/actions');
  const acquirePosts=()=>posts.filter(p=>p.path==='/api/control/acquire');
  await until("document.querySelector('[data-preset-index=\"0\"]')",'presets');
  await click('[data-preset-index="0"]');
  await until("document.querySelector('#pose-plan-summary').textContent.includes('执行前需现场确认')",'recovery review');
  await wait(900);assert.strictEqual(actionPosts().length,0);assert.strictEqual(acquirePosts().length,0);
  await click('[data-click="execute-current"]');await until("document.querySelector('#execute-dialog').open",'recovery confirmation');
  let description=await evaluate("document.querySelector('#execute-description').textContent");
  ['-27.64','-25','head_j2','没有软件速度保证','不能撤回','阻止后续动作'].forEach(text=>assert(description.includes(text)));
  assert.strictEqual(await evaluate("[...document.querySelectorAll('[data-target-input],[data-preset-index]')].every(x=>x.disabled)"),true);
  await snap('confirmation');await click('#cancel-execute');await wait(150);
  assert.strictEqual(actionPosts().length,0);assert.strictEqual(acquirePosts().length,0);
  await click('[data-click="execute-current"]');await until("document.querySelector('#execute-dialog').open",'confirmed run');
  await evaluate("document.querySelector('#execute-code').value='mock-only';document.querySelector('#execute-form').requestSubmit()");
  await until("document.body.classList.contains('control-mode')&&!document.querySelector('#execute-dialog').open",'one authorization');
  assert.strictEqual(actionPosts().length,1);assert.strictEqual(acquirePosts().length,1);
  assert.strictEqual(actionPosts()[0].body.head_boundary_recovery_ack,true);
  assert.deepStrictEqual(actionPosts()[0].body.targets,target);
  current.status='running';current.progress_detail={phase:'head_boundary_observation',speed_controlled:false,boundary_command_sent:true};
  await until("document.querySelector('#pose-plan-summary').textContent==='正在回到工作范围'",'recovery-specific progress');
  assert.strictEqual(await evaluate("document.querySelector('#stop-button').textContent"),'取消后续动作');
  assert.strictEqual(await evaluate("document.querySelector('#pose-plan-summary').textContent.includes('可随时停止')"),false);
  await snap('observing');await click('#stop-button');
  await until("document.querySelector('#pose-plan-summary').textContent.includes('后续动作已取消')",'cancellation semantics');
  assert.strictEqual(await evaluate("document.querySelector('#pose-plan-summary').textContent.includes('已停止')"),false);
  assert.strictEqual(actionPosts().length,1);await snap('cancelled');
  await click('[data-preset-index="0"]');await until("!document.querySelector('[data-click=\"execute-current\"]').disabled",'retry ready');
  await click('[data-click="execute-current"]');await until("document.querySelector('#execute-dialog').open",'repeat recovery still requires explicit confirmation');
  assert.strictEqual(await evaluate("document.querySelector('#execute-code').hidden&&document.querySelector('#execute-code').disabled&&!document.querySelector('#execute-code').required"),true);
  assert.strictEqual(actionPosts().length,1);assert.strictEqual(acquirePosts().length,1);
  await click('#cancel-execute');await wait(100);assert.strictEqual(actionPosts().length,1);
  await click('[data-click="execute-current"]');await until("document.querySelector('#execute-dialog').open",'second confirmed recovery');
  await evaluate("const input=document.querySelector('[data-target-input=\"body_j3\"]');input.value='-30';input.dispatchEvent(new Event('input',{bubbles:true}));");
  await click('#confirm-execute');await until("!document.querySelector('#execute-dialog').open",'second task submitted');
  assert.strictEqual(actionPosts().length,2);assert.strictEqual(acquirePosts().length,1);
  assert.strictEqual(actionPosts()[1].body.head_boundary_recovery_ack,true);assert.deepStrictEqual(actionPosts()[1].body.targets,target);
  headPosition=0;current.status='succeeded';current.result={commands_sent:true,feedback_verified:true};
  await until("document.querySelector('#pose-scope').textContent.includes('已到位')",'whole recovery task completion');
  recovery.origin=27.64;recovery.edge=25;recovery.direction=-1;headPosition=recovery.origin;
  await click('[data-preset-index="0"]');await until("document.querySelector('#pose-plan-summary').textContent.includes('现场确认')",'upper boundary plan');
  await click('[data-click="execute-current"]');await until("document.querySelector('#execute-dialog').open",'upper boundary explicit confirmation');
  description=await evaluate("document.querySelector('#execute-description').textContent");
  assert(description.includes('当前 27.64°')&&description.includes('目标 25°')&&!description.includes('-25°'));
  await click('#confirm-execute');await until("!document.querySelector('#execute-dialog').open",'upper boundary submitted');
  assert.strictEqual(actionPosts().length,3);assert.strictEqual(actionPosts()[2].body.head_boundary_recovery_ack,true);assert.strictEqual(acquirePosts().length,1);
  headPosition=0;current.status='succeeded';current.result={commands_sent:true,feedback_verified:true};
  await until("document.querySelector('#pose-scope').textContent.includes('已到位')",'upper boundary completion');
  recovery.origin=-27.64;recovery.edge=-25;recovery.direction=1;headPosition=recovery.origin;
  mode='ordinary';await click('[data-preset-index="1"]');await until("!document.querySelector('[data-click=\"execute-current\"]').disabled",'ordinary run');
  await click('[data-click="execute-current"]');await until("document.querySelector('#pose-scope').textContent.includes('正在')",'ordinary existing control unchanged');
  assert.strictEqual(actionPosts().length,4);assert.strictEqual(acquirePosts().length,1);
  assert.strictEqual(Object.prototype.hasOwnProperty.call(actionPosts()[3].body,'head_boundary_recovery_ack'),false);
  assert.strictEqual(await evaluate("document.querySelector('#execute-dialog').open"),false);
  headPosition=0;current.status='succeeded';current.result={commands_sent:true,feedback_verified:true};
  await until("document.querySelector('#pose-scope').textContent.includes('已到位')",'ordinary completion');
  mode='late';await click('[data-preset-index="0"]');await until("document.querySelector('#pose-plan-summary').textContent.includes('8.2')",'normal review before late recovery');
  await click('[data-click="execute-current"]');await until("document.querySelector('#pose-plan-summary').textContent.includes('重新点击执行并确认')",'new recovery must not bypass confirmation');
  assert.strictEqual(actionPosts().length,4);assert.strictEqual(acquirePosts().length,1);
  mode='invalid';await click('[data-preset-index="0"]');await wait(450);await click('[data-click="execute-current"]');
  await until("document.querySelector('#pose-plan-summary').textContent.includes('计划不完整')",'invalid recovery data blocked');
  assert.strictEqual(actionPosts().length,4);
  mode='recovery';await click('[data-preset-index="0"]');await wait(450);await click('[data-click="execute-current"]');await until("document.querySelector('#execute-dialog').open",'expired lease setup');
  lease=null;await until("!document.body.classList.contains('control-mode')",'lease expiry');
  await click('#confirm-execute');await until("!document.querySelector('#execute-code').hidden",'expired lease requires code without submitting');
  assert.strictEqual(actionPosts().length,4);assert.strictEqual(acquirePosts().length,1);
  await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await wait(150);
  assert.strictEqual(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);await snap('mobile-confirmation');
  await click('#cancel-execute');assert.strictEqual(actionPosts().length,4);assert.deepStrictEqual(exceptions,[]);
  const report={result:'passed',source:'isolated candidate mock only; no robot connection or profile change',checks:['GET review creates no action or authorization','explicit boundary target and uncontrolled speed warning','cancel cannot retract boundary target disclosure','cancel before confirmation creates no action','pending targets frozen','ack sent only after confirmation','one authorization reused for explicit recovery confirmations','upper boundary validates inward direction and confirms +25 target','head_boundary_observation has no stop guarantee','cancel reports only subsequent action cancellation','ordinary action unchanged and no ack leakage','late plan recovery cannot auto-submit','invalid recovery plan rejected','expired lease cannot silently authorize','mobile no overflow','no browser exceptions'],action_posts:actionPosts().length,acquire_posts:acquirePosts().length};
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
 }finally{if(ws)ws.close();chrome.kill('SIGTERM');server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});

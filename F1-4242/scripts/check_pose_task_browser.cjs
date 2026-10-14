/* Isolated browser regression. Serves local assets and a fake API on a random
 * loopback port. No request is routed to a robot or a running workbench. */
const http = require('http'), fs = require('fs'), path = require('path');
const assert = require('assert'), cp = require('child_process'), os = require('os');
const WebSocket = require('ws');
const root = path.resolve(__dirname, '..'), output = process.env.F1_POSE_BROWSER_OUTPUT || path.join(root, 'results/pose_task_browser');
const profile = JSON.parse(fs.readFileSync(path.join(root, 'configs/f1-09.json')));
const groups = {left_arm:[],right_arm:[],body:[],head:[]};
Object.keys(profile.joint_limits_deg).forEach(n => groups[n.startsWith('arm_l')?'left_arm':n.startsWith('arm_r')?'right_arm':n.startsWith('body')?'body':'head'].push(n));
const target = Object.assign({}, ...Object.keys(profile.joint_limits_deg).map(n => ({[n]:0})), {body_j3:-5,body_j4:-10,body_j5:-5});
const robot = {id:'F1-test',label:'模拟机器人',groups:groups,joint_limits_deg:profile.joint_limits_deg,capabilities:{joints:{available:true},gripper:{available:true},base:{available:true}}};
let authenticated=false, lease=null, current=null, posts=[], mode='ready', serial=0;
const plan = {motion_s:8.03,estimated_total_s:8.18,parts:[{label:'左臂',status:'moving'},{label:'右臂',status:'moving'},{label:'身体',status:'moving'},{label:'头部',status:'moving'}],checkpoint_count:2};
function state(){return {connected:true,authenticated:authenticated,control:{lease_id:lease,owner:lease?'browser':null},joints:Object.assign({},...Object.keys(target).map(n=>({[n]:{position:target[n],age_s:.01}}))),grippers:{left:{position:20,age_s:.01},right:{position:20,age_s:.01}},cameras:{},services:{},capabilities:robot.capabilities};}
const server = http.createServer(async (req,res)=>{
 const url = new URL(req.url,'http://127.0.0.1');let raw='';for await(const chunk of req)raw+=chunk;
 const body=raw?JSON.parse(raw):null;if(req.method==='POST')posts.push({path:url.pathname,body:body});
 let data;
 if(url.pathname==='/api/robot')data={robot:robot};
 else if(url.pathname==='/api/session')data={authenticated:authenticated};
 else if(url.pathname==='/api/state')data=state();
 else if(url.pathname==='/api/system')data={system:{version:'test'}};
 else if(url.pathname==='/api/presets')data={presets:[{id:'reset',name:'全身零位 · 原生 pose_2',source:'native_pose_2',joints:target},{id:'arms',name:'双臂复位',source:'lxl_xy_reset',joints:Object.assign({},...groups.left_arm.concat(groups.right_arm).map(n=>({[n]:target[n]})))}]};
 else if(url.pathname==='/api/pose/review')data={review:mode==='blocked'?{executable:false,issues:[{code:'origin_out_of_range',message:'head_j2 实测 -27.64°，超出配置范围 -25° ~ 25°。驱动会截断越界命令，需先核对该关节的标定与原生恢复方式。'}]}:{executable:true,plan:plan,issues:[]}};
 else if(url.pathname==='/api/actions/plan')data={plan:plan};
 else if(url.pathname==='/api/control/acquire'){authenticated=true;lease='mock-browser-lease';data={authenticated:true,lease_id:lease};}
 else if(url.pathname==='/api/control/release'){lease=null;data={ok:true};}
 else if(url.pathname==='/api/control/heartbeat')data={ok:true};
 else if(url.pathname==='/api/actions'){current={id:'mock-'+(++serial),status:'accepted',progress:0};data={action:current};}
 else if(url.pathname.startsWith('/api/actions/'))data={action:current};
 else if(url.pathname==='/api/stop'){current=Object.assign({},current,{status:'stopped',result:{commands_sent:true}});data={action:current};}
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
  const until=async(expression,label)=>{for(let i=0;i<80;i++){if(await evaluate(expression))return;await wait(100);}throw Error('Timed out: '+label);};
  const click=selector=>evaluate('document.querySelector('+JSON.stringify(selector)+').click()');
  const snap=async name=>{const r=await call('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(output,name+'.png'),Buffer.from(r.data,'base64'));};
  await call('Runtime.enable');await call('Page.enable');await call('Emulation.setDeviceMetricsOverride',{width:1680,height:1100,deviceScaleFactor:1,mobile:false});
  await call('Page.navigate',{url:'http://127.0.0.1:'+port+'/#control'});
  await until("document.querySelector('[data-preset-index=\"0\"]')",'presets');
  await click('[data-preset-index="0"]');await until("document.querySelector('#pose-plan-summary').textContent.includes('8.2')",'whole task estimate');
  assert.strictEqual(await evaluate("document.querySelector('#pose-scope').textContent"),'全身复位');
  assert.strictEqual(await evaluate("document.body.innerText.includes('下一段')||document.body.innerText.includes('第 1 段')||!!document.querySelector('#segment-dialog')"),false);
  await snap('ready');await click('[data-click="execute-current"]');await until("document.querySelector('#execute-dialog').open",'authorization dialog');
  await evaluate("document.querySelector('#execute-code').value='mock-only';document.querySelector('#execute-form').requestSubmit()");
  await until("document.body.classList.contains('control-mode')&&!document.querySelector('#execute-dialog').open",'control mode');
  assert.strictEqual(posts.filter(p=>p.path==='/api/actions').length,1);
  assert.strictEqual(Object.keys(posts.find(p=>p.path==='/api/actions').body.targets).length,22);
  assert.strictEqual(await evaluate("[...document.querySelectorAll('[data-target-input],[data-preset-index]')].every(x=>x.disabled)"),true);
  assert.strictEqual(await evaluate("document.querySelector('#stop-button').disabled"),false);
  current.status='running';current.progress=.55;await until("document.querySelector('progress')?.value>.5",'whole task progress');
  await snap('running');await click('[data-click="execute-current"]');assert.strictEqual(posts.filter(p=>p.path==='/api/actions').length,1);
  current.status='succeeded';current.progress=1;current.result={feedback_verified:true,commands_sent:true,parts:plan.parts.map(p=>Object.assign({},p,{status:'at_target'}))};
  await until("document.querySelector('#pose-scope').textContent.includes('已到位')",'verified completion');
  assert.strictEqual(await evaluate("document.querySelector('#toast-region').children.length"),0);
  await snap('completed');await click('[data-preset-index="1"]');await until("!document.querySelector('[data-click=\"execute-current\"]').disabled",'second execution');
  await click('[data-click="execute-current"]');await until("document.querySelector('#pose-scope').textContent.includes('正在')",'second task');
  assert.strictEqual(posts.filter(p=>p.path==='/api/actions').length,2);assert.strictEqual(posts.filter(p=>p.path==='/api/control/acquire').length,1);
  await click('#stop-button');await until("document.querySelector('#pose-plan-summary').textContent.includes('已停止')",'stop');
  assert.strictEqual(posts.filter(p=>p.path==='/api/actions').length,2);
  await click('[data-preset-index="1"]');await until("!document.querySelector('[data-click=\"execute-current\"]').disabled",'unverified test ready');
  await click('[data-click="execute-current"]');await until("document.querySelector('#pose-scope').textContent.includes('正在')",'third task');
  current.status='succeeded';current.result={commands_sent:true,feedback_verified:false};
  await until("document.querySelector('#pose-plan-summary').textContent.includes('尚未确认到位')",'unverified success stays unconfirmed');
  assert.strictEqual(await evaluate("document.querySelectorAll('.joint-editor.edited').length>0"),true);
  plan.no_motion=true;plan.motion_s=0;plan.estimated_total_s=.15;
  await click('[data-preset-index="0"]');await until("document.querySelector('#pose-plan-summary').textContent.includes('执行后确认到位')",'no-motion preflight is not arrival');
  assert.strictEqual(await evaluate("document.querySelector('#pose-scope').textContent.includes('已到位')"),false);
  await click('[data-click="execute-current"]');await until("document.querySelector('#pose-scope').textContent.includes('正在')",'no-motion submits one task');
  assert.strictEqual(posts.filter(p=>p.path==='/api/actions').length,4);
  current.status='succeeded';current.result={commands_sent:false,no_motion:true};
  await until("document.querySelector('#pose-plan-summary').textContent.includes('尚未确认到位')",'no-motion without fresh verification stays unconfirmed');
  assert.strictEqual(await evaluate("document.querySelectorAll('.joint-editor.edited').length===22"),true);
  assert.strictEqual(await evaluate("document.querySelector('#pose-scope').textContent.includes('已到位')"),false);
  await click('[data-click="execute-current"]');await until("document.querySelector('#pose-scope').textContent.includes('正在')",'no-motion observation retry');
  assert.strictEqual(posts.filter(p=>p.path==='/api/actions').length,5);
  current.status='running';current.progress=.7;await wait(900);
  assert.strictEqual(await evaluate("document.querySelectorAll('.joint-editor.edited').length===22"),true);
  current.status='succeeded';current.result={commands_sent:false,no_motion:true,feedback_verified:true};
  await until("document.querySelector('#pose-scope').textContent.includes('已到位')",'no-motion only confirmed after fresh observation');
  assert.strictEqual(await evaluate("document.querySelectorAll('.joint-editor.edited').length"),0);
  assert.strictEqual(posts.filter(p=>p.path==='/api/control/acquire').length,1);
  mode='blocked';await click('[data-preset-index="0"]');await until("document.querySelector('#pose-plan-summary').textContent.includes('头部当前状态')",'head recovery blocker');
  await click('[data-click="execute-current"]');await wait(300);
  assert.strictEqual(posts.filter(p=>p.path==='/api/actions').length,5);assert.strictEqual(await evaluate("document.querySelector('#toast-region').children.length"),0);
  await click('.pose-diagnostics summary');await wait(900);assert.strictEqual(await evaluate("document.querySelector('.pose-diagnostics').open"),true);
  await snap('blocked');await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await wait(200);
  assert.strictEqual(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);await snap('mobile');
  assert.deepStrictEqual(exceptions,[]);
  const report={result:'passed',source:'isolated mocked API; no robot connection',checks:['22-joint preset single submission','one authorization across two actions','8.18-second whole-task estimate','no manual checkpoints','running edits and presets locked','whole-task progress','feedback-only success','unverified result retains target and never says arrived','no-motion preflight still submits whole task','no-motion without verification retains draft','no-motion arrives only with fresh feedback confirmation','stop does not continue','head blocker produces no action','single inline error','expanded diagnostics remain open','mobile no overflow','no browser exceptions'],action_posts:posts.filter(p=>p.path==='/api/actions').length,acquire_posts:posts.filter(p=>p.path==='/api/control/acquire').length};
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
 }finally{if(ws)ws.close();chrome.kill('SIGTERM');server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});

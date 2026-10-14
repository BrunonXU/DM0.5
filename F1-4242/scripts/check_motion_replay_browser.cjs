/* Isolated browser regression. Serves local assets and a fake API on a random
 * loopback port. No request is routed to a robot or a running workbench. */
const http = require('http'), fs = require('fs'), path = require('path');
const assert = require('assert'), cp = require('child_process'), os = require('os');
const WebSocket = require('ws');
const root = path.resolve(__dirname, '..'), output = process.env.F1_REPLAY_BROWSER_OUTPUT || path.join(root, 'results/motion_replay_browser');
const webRoot = process.env.F1_REPLAY_BROWSER_WEB_ROOT || path.join(root,'web');
const profile = JSON.parse(fs.readFileSync(path.join(root, 'configs/f1-09.json')));
const groups = {left_arm:[],right_arm:[],body:[],head:[]};
Object.keys(profile.joint_limits_deg).forEach(n => groups[n.startsWith('arm_l')?'left_arm':n.startsWith('arm_r')?'right_arm':n.startsWith('body')?'body':'head'].push(n));
const target = Object.assign({}, ...Object.keys(profile.joint_limits_deg).map(n => ({[n]:0})), {body_j3:-5,body_j4:-10,body_j5:-5});
const robot = {id:'F1-test',label:'模拟机器人',groups:groups,joint_limits_deg:profile.joint_limits_deg,capabilities:{joints:{available:true},gripper:{available:true},base:{available:true}}};
let authenticated=false, lease=null, current=null, posts=[], mode='ready', serial=0;
const eid='eeeeeeeeeeeeeeeeeeeeeeee';
const episode={id:eid,name:'f1_hil_fixture',source:'hil_demonstration',execution:'powered',recording_status:'committed',source_signature:'fixture',files:[],topics:[],videos:[],duration_s:50.3,review:{status:'pending'}};
function replayReview(){return {episode_id:eid,episode_name:episode.name,review_id:'review-fixture',executable:mode==='ready',can_align:mode==='misaligned',issues:mode==='ready'?[]:['回放起点尚未对齐'],valid_for_s:120,start_s:0,end_s:50.3,duration_s:50.3,total_duration_s:50.3,command_count:10366,initial_targets:Object.fromEntries(groups.left_arm.concat(groups.right_arm).map(n=>[n,1])),initial_grippers:{left:0,right:0},context:Object.fromEntries(groups.head.concat(groups.body).map(n=>[n,target[n]])),events:[{time_s:31,event:'home_stopped',reason:'历史跟踪误差',selected:true}]};}
const plan = {motion_s:8.03,estimated_total_s:8.18,parts:[{label:'左臂',status:'moving'},{label:'右臂',status:'moving'},{label:'身体',status:'moving'},{label:'头部',status:'moving'}],checkpoint_count:2};
function state(){return {connected:true,authenticated:authenticated,control:{lease_id:lease,owner:lease?'browser':null},joints:Object.assign({},...Object.keys(target).map(n=>({[n]:{position:target[n],age_s:.01}}))),grippers:{left:{position:20,age_s:.01},right:{position:20,age_s:.01}},cameras:{},services:{},capabilities:robot.capabilities,action:current};}
const server = http.createServer(async (req,res)=>{
 const url = new URL(req.url,'http://127.0.0.1');let raw='';for await(const chunk of req)raw+=chunk;
 const body=raw?JSON.parse(raw):null;if(req.method==='POST')posts.push({path:url.pathname,body:body});
 let data;
 if(url.pathname==='/api/robot')data={robot:robot};
 else if(url.pathname==='/api/session')data={authenticated:authenticated};
 else if(url.pathname==='/api/state')data=state();
 else if(url.pathname==='/api/data')data={episodes:[episode],available:true,summary:{episode_count:1},recording:{state:'no_process_observed'}};
 else if(url.pathname==='/api/data/episodes/'+eid)data={episode:episode};
 else if(url.pathname==='/api/replay/review')data={review:replayReview()};
 else if(url.pathname==='/api/replay/align'){current={id:'align-'+(++serial),kind:'pose',status:'running',progress:.2,replay:{episode_id:eid,phase:'alignment'}};data={action:current};}
 else if(url.pathname.startsWith('/api/replay/reports/'))data={report:{result:{status:current.status,dispatch_timing:{mean_lateness_ms:.1,max_lateness_ms:.4}},comparison:{available:true,verdict:'偏差统计，不自动判定硬件合格',channels:groups.left_arm.concat(groups.right_arm).map(n=>({name:n,unit:'deg',matched:3,replay_samples:3,metrics:{mean_abs:.1,max_abs:.2,max_at_s:1},points:[[0,0,.1],[1,1,1.2],[2,2,2.1]]}))}}};
 else if(url.pathname==='/api/replay/start'){current={id:'replay-'+(++serial),kind:'replay',status:'running',progress:.2,progress_detail:{elapsed_s:10,estimated_total_s:50.3,max_arm_error_deg:.1},replay:{episode_id:eid,report_id:'fixture-report'}};data={action:current};}
 else if(url.pathname==='/api/system')data={system:{version:'test'}};
 else if(url.pathname==='/api/presets')data={presets:[{id:'reset',name:'全身零位 · 原生 pose_2',source:'native_pose_2',joints:target},{id:'arms',name:'双臂复位',source:'lxl_xy_reset',joints:Object.assign({},...groups.left_arm.concat(groups.right_arm).map(n=>({[n]:target[n]})))}]};
 else if(url.pathname==='/api/pose/review')data={review:mode==='blocked'?{executable:false,issues:[{code:'origin_out_of_range',message:'head_j2 实测 -27.64°，超出配置范围 -25° ~ 25°。驱动会截断越界命令，需先核对该关节的标定与原生恢复方式。'}]}:{executable:true,plan:plan,issues:[]}};
 else if(url.pathname==='/api/actions/plan')data={plan:plan};
 else if(url.pathname==='/api/control/acquire'){authenticated=true;lease='mock-browser-lease';data={authenticated:true,lease_id:lease};}
 else if(url.pathname==='/api/control/release'){lease=null;data={ok:true};}
 else if(url.pathname==='/api/control/heartbeat')data={ok:true};
 else if(url.pathname==='/api/actions'){current={id:'mock-'+(++serial),status:'accepted',progress:0};data={action:current};}
 else if(url.pathname.startsWith('/api/actions/'))data={action:current};
 else if(url.pathname==='/api/stop'){current=Object.assign({},current,{status:'cancelled',result:{commands_sent:true,feedback_verified:false}});data={action:current};}
 else if(url.pathname==='/api/dev/revision')data={revision:'test'};
 else if(url.pathname==='/api/events')data={events:[]};
 else if(url.pathname.startsWith('/api/'))data={};
 if(data!==undefined){res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));return;}
 const rel=url.pathname==='/'?'index.html':decodeURIComponent(url.pathname).slice(1),file=path.resolve(webRoot,rel);
 if(!file.startsWith(webRoot+path.sep)||!fs.existsSync(file)){res.statusCode=404;res.end();return;}
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
  await call('Page.navigate',{url:'http://127.0.0.1:'+port+'/#data?episode='+eid});
  await until("document.querySelector('[data-motion-replay-open]')",'Replay entry');
  assert.strictEqual(posts.length,0);
  await click('[data-motion-replay-open]');
  await until("document.querySelector('[data-r-ready]')",'read-only review');
  assert.strictEqual(posts.length,0);
  assert.strictEqual(await evaluate("document.querySelector('[data-r-more]').open"),false);
  assert.strictEqual(await evaluate("document.querySelector('[data-r-diagnostics]').open"),false);
  assert.strictEqual(await evaluate("document.querySelector('[data-r-start-task]').disabled"),false);
  assert.strictEqual(await evaluate("document.querySelector('[data-r-align]').hidden"),false);
  assert.strictEqual(await evaluate("document.querySelector('[data-r-align]').disabled"),true);
  assert.strictEqual(await evaluate("document.querySelector('[data-motion-replay]').getBoundingClientRect().height<380"),true);
  assert.strictEqual(await evaluate("document.querySelector('[data-r-duration]').textContent"),'50.300');
  assert.strictEqual(await evaluate("document.querySelector('[data-motion-replay-open]').getAttribute('aria-expanded')"),'true');
  assert.strictEqual(await evaluate("document.querySelector('[data-motion-replay-open]').classList.contains('primary')"),false);
  await click('[data-r-close]');
  assert.strictEqual(await evaluate("document.querySelector('[data-motion-replay-open]').getAttribute('aria-expanded')"),'false');
  assert.strictEqual(await evaluate("document.querySelector('[data-motion-replay-open]').classList.contains('primary')"),true);
  await click('[data-motion-replay-open]');await until("document.querySelector('[data-r-ready]')",'reopening remains read-only');
  assert.strictEqual(posts.length,0);
  await snap('prepare');
  await click('[data-r-start-task]');await until("document.querySelector('#control-code-dialog').open",'automatic control code prompt');
  await evaluate("document.querySelector('#control-code').value='fixture';document.querySelector('#control-code-form').requestSubmit()");
  await until("!!document.querySelector('[data-r-cancel]')",'single motion confirmation');
  assert.strictEqual(await evaluate("document.querySelector('[data-r-cancel]').closest('form').querySelectorAll('input[type=checkbox]').length"),0);
  await click('[data-r-cancel]');assert.strictEqual(posts.filter(p=>p.path==='/api/replay/start').length,0);
  await click('[data-r-start-task]');await until("!!document.querySelector('[data-r-cancel]')",'confirmation again');
  await evaluate("document.querySelector('[data-r-cancel]').closest('form').requestSubmit()");
  await until("document.querySelector('[data-r-status]').textContent.includes('正在回放')",'running');
  assert.strictEqual(posts.filter(p=>p.path==='/api/replay/start').length,1);
  assert.strictEqual(await evaluate("document.querySelector('[data-r-start]').disabled && document.querySelector('[data-r-start-task]').disabled"),true);
  assert.strictEqual(await evaluate("document.querySelector('[data-r-review]').hidden"),true);
  await snap('running');
  await click('[data-r-stop]');await until("document.querySelector('[data-r-status]').textContent.includes('已停止')",'stopped');
  await until("!!document.querySelector('[data-r-channel]')",'actual comparison report');
  assert.strictEqual(await evaluate("document.querySelector('[data-r-channel]').closest('details').open"),false);
  await evaluate("document.querySelector('[data-r-channel]').closest('details').open=true");
  assert.strictEqual(await evaluate("document.querySelectorAll('[data-r-chart] svg path').length"),3);
  assert.strictEqual(await evaluate("(()=>{const t=document.querySelector('.replay-result .table-scroll');return t.clientHeight<310&&t.scrollHeight>t.clientHeight})()"),true);
  await evaluate("document.querySelector('[data-r-comparison]').scrollIntoView({block:'start'})");await wait(200);
  assert.strictEqual(await evaluate("document.querySelector('[data-r-chart] svg').getBoundingClientRect().height>150"),true);
  await snap('comparison');
  await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await wait(200);
  assert.strictEqual(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);await snap('mobile');
  mode='misaligned';await click('[data-r-check]');await until("document.querySelector('[data-r-review]').textContent.includes('尚未对齐')",'blocked start');
  await click('[data-r-align]');await until("!!document.querySelector('[data-r-cancel]')",'alignment confirmation in place');
  assert.strictEqual(await evaluate("location.hash.startsWith('#data')"),true);
  assert.strictEqual(posts.filter(p=>p.path==='/api/replay/align').length,0);
  await evaluate("document.querySelector('[data-r-cancel]').closest('form').requestSubmit()");
  await until("document.querySelector('[data-r-status]').textContent.includes('正在回到起点')",'alignment running');
  assert.strictEqual(posts.filter(p=>p.path==='/api/replay/align').length,1);
  current.status='succeeded';mode='ready';await until("!!document.querySelector('[data-r-ready]')",'automatic recheck after alignment');
  assert.strictEqual(posts.filter(p=>p.path==='/api/replay/start').length,1); // NEVER start automatically
  await evaluate("window.originalDateNow=Date.now;Date.now=()=>originalDateNow()+180000");
  await until("document.querySelector('[data-r-ready]').textContent.includes('过期')",'expired review explained');
  assert.strictEqual(await evaluate("document.querySelector('[data-r-start-task]').disabled"),false);
  await evaluate("Date.now=originalDateNow");
  assert.deepStrictEqual(exceptions,[]);
  const report={result:'passed',source:'isolated fake API; no robot connection',checks:['read-only opening and reopening','whole original speed default','range and diagnostics collapsed','compact control panel below 380px','origin action remains visible but disabled when ready','entry yields primary emphasis to start while expanded','automatic control authorization','single motion confirmation','cancel sends no motion','no redundant event checkbox','single start','readiness label hidden during movement','stop without resume','actual comparison and curves','comparison details collapsed with bounded table height','mobile no overflow','in-place alignment confirmation','no automatic replay after alignment','expired review explained and automatically refreshed on start','no browser exceptions']};
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
 }finally{if(ws)ws.close();chrome.kill('SIGTERM');server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});

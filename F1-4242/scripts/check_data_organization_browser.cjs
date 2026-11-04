/* Isolated browser regression. Serves local assets and a fake API on a random
 * loopback port. No request is routed to a robot or a running workbench. */
const http = require('http'), fs = require('fs'), path = require('path');
const assert = require('assert'), cp = require('child_process'), os = require('os');
const WebSocket = require('ws');
const root = path.resolve(__dirname, '..'), output = process.env.F1_UIUX_BROWSER_OUTPUT || path.join(root, 'results/data-version-implementation/browser');
const profile = JSON.parse(fs.readFileSync(path.join(root, 'configs/f1-09.json')));
const groups = {left_arm:[],right_arm:[],body:[],head:[]};
Object.keys(profile.joint_limits_deg).forEach(n => groups[n.startsWith('arm_l')?'left_arm':n.startsWith('arm_r')?'right_arm':n.startsWith('body')?'body':'head'].push(n));
const target = Object.assign({}, ...Object.keys(profile.joint_limits_deg).map(n => ({[n]:0})), {body_j3:-5,body_j4:-10,body_j5:-5});
const robot = {id:'F1-test',label:'模拟机器人',groups:groups,joint_limits_deg:profile.joint_limits_deg,capabilities:{joints:{available:true},gripper:{available:true},base:{available:true}}};
let organization={schema:1,revision:'0',folders:[],memberships:{}},orgSequence=0,failOrganization=null,orgDelay=0;
let authenticated=true, lease=null, current=null, posts=[], mode='ready', serial=0;
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
 else if(url.pathname==='/api/data')data={organization:organization,available:true,episodes:Array.from({length:137},(_,i)=>({id:i.toString(16).padStart(24,'0'),name:'f1_hil_20261013T140457Z_'+String(i).padStart(8,'0'),path:'/data/hil_data/demonstration/'+i,source:'hil_demonstration',source_signature:'fixture',started_at:'2026-10-13T14:04:57Z',size_bytes:479200000,file_count:10,recording_status:'committed',review:{status:'pending',revision:'none'}})),summary:{episode_count:137}};
 else if(url.pathname==='/api/data/organization'){
  await new Promise(r=>setTimeout(r,orgDelay));
  if(failOrganization){res.statusCode=409;data={ok:false,error:'目录已被其他操作更新，请刷新后重试',code:'organization_conflict'};}
  else if(body.expected_revision!==organization.revision){res.statusCode=409;data={ok:false,error:'目录冲突'};}
  else{
   const next=JSON.parse(JSON.stringify(organization));
   if(body.operation==='create')next.folders.push({id:'@'+String(++orgSequence).padStart(32,'0'),parent:body.parent,name:body.name});
   else if(body.operation==='rename')next.folders.find(f=>f.id===body.folder_id).name=body.name;
   else if(body.folder_id)next.folders.find(f=>f.id===body.folder_id).parent=body.target;
   else body.episodes.forEach(e=>next.memberships[e.id]={folder:body.target,source_signature:e.source_signature});
   next.revision=String(Number(next.revision)+1);organization=next;data={ok:true,organization:organization};
  }
 }
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

  const checks=[];
  await call('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false});
  await call('Page.navigate',{url:'http://127.0.0.1:'+port+'/#data'});
  await until("document.querySelector('#data-version-tree [data-folder=DAgger]')",'directory');
  await click('#data-version-tree [data-folder="DAgger/人工示教"]');
  await until("document.querySelectorAll('#data-folders [data-episode]').length===8",'page');
  const create=async name=>{await click('[data-organize=create]');await evaluate("document.querySelector('#organization-name').value="+JSON.stringify(name));await click('.data-organization-dialog [type=submit]');await until("!document.querySelector('.data-organization-dialog')",'created');await wait(150);};
  await click('[data-organize=create]');await snap('new-folder');await click('.data-organization-dialog [data-cancel]');
  await create('V1_基线');await create('V2_补录');
  assert.strictEqual(organization.folders.length,2);const v1=organization.folders[0].id,v2=organization.folders[1].id;
  await snap('browse');checks.push('named empty folders created');
  const drag=async(source,target)=>{
   await evaluate(`{const dt=new DataTransfer(),from=document.querySelector(${JSON.stringify(source)}),to=document.querySelector(${JSON.stringify(target)});from.dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:dt}));to.dispatchEvent(new DragEvent('dragover',{bubbles:true,cancelable:true,dataTransfer:dt}));window.fixtureDrop={dt,to};}`);
   await snap('drag');assert(await evaluate("document.querySelectorAll('.drag-target').length>0"),'drop highlight');
   await evaluate("window.fixtureDrop.to.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:window.fixtureDrop.dt}))");await wait(500);
  };
  const eid=await evaluate("document.querySelector('#data-folders [data-drag-episode]').dataset.dragEpisode");
  await drag('#data-folders [data-drag-episode]','#data-version-tree [data-folder="'+v1+'"]');
  assert.strictEqual(organization.memberships[eid].folder,v1);checks.push('recording drag preserves ID and signature');
  await drag('#data-folders [data-drag-folder="'+v2+'"]','#data-version-tree [data-folder="'+v1+'"]');
  assert.strictEqual(organization.folders[1].parent,v1);checks.push('nested custom folder drag');
  await click('#data-version-tree [data-folder="'+v1+'"]');
  await until("document.querySelector('#data-folders [data-episode]')",'moved record');
  assert.strictEqual(await evaluate("document.querySelector('#data-folders [data-episode]').dataset.episode"),eid);
  await click('[data-organize=rename]');await evaluate("document.querySelector('#organization-name').value='V1_验收'");await click('.data-organization-dialog [type=submit]');await until("!document.querySelector('.data-organization-dialog')",'rename');
  await call('Page.reload');await until("document.querySelector('#data-version-tree')?.textContent.includes('V1_验收')",'persistent folders');
  assert(await evaluate("document.querySelector('.data-breadcrumb').textContent.includes('V1_验收')"),'stable folder navigation after rename/reload');checks.push('rename and refresh preserve current folder and hierarchy');
  await click('#data-version-tree [data-folder="DAgger/人工示教"]');
  await click('[data-click=data-select-start]');await click('[data-click=data-select-all]');
  await click('[data-data-page="2"]');await click('[data-click=data-select-all]');
  await click('[data-organize=move-selected]');await evaluate("document.querySelector('#organization-target').value="+JSON.stringify(v2));
  orgDelay=500;await click('.data-organization-dialog [type=submit]');
  assert(await evaluate("document.querySelector('.data-organization-dialog [type=submit]').disabled"),'double submit guarded');
  await until("!document.querySelector('.data-organization-dialog')",'batch move');orgDelay=0;
  assert.strictEqual(Object.values(organization.memberships).filter(m=>m.folder===v2).length,15);checks.push('cross-page selection moves 15 recordings (one folder occupies a page slot) atomically');
  await click('[data-click=data-select-cancel]');failOrganization=true;
  await click('[data-organize=create]');await evaluate("document.querySelector('#organization-name').value='保留输入'");await click('.data-organization-dialog [type=submit]');
  await until("document.querySelector('.data-dialog-error')&&!document.querySelector('.data-dialog-error').hidden",'conflict');
  assert.strictEqual(await evaluate("document.querySelector('#organization-name').value"),'保留输入');
  assert(await evaluate("document.querySelector('.data-breadcrumb').textContent.includes('人工示教')"),'conflict retains position');await snap('conflict');await click('.data-organization-dialog [data-cancel]');failOrganization=null;checks.push('conflict keeps input and position; no automatic retry');
  for(const [width,height] of [[1920,1080],[1440,900],[1280,800]]){
   await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});await snap('data-'+width);
   assert(await evaluate('document.documentElement.scrollWidth<=innerWidth'),'desktop overflow');
   assert(await evaluate("document.querySelector('#data-version-tree').getBoundingClientRect().bottom<innerHeight+200"),'versions next to files');
  }
  checks.push('three desktop sizes, permanent storage, compact list and version tree');
  assert.strictEqual(exceptions.length,0,JSON.stringify(exceptions));
  assert(posts.every(p=>p.path==='/api/data/organization'),'only classification writes');
  const report={result:'passed',checks,exceptions,posts,source:'isolated fake API; desktop only; actual model resources'};
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
 }finally{if(ws)ws.close();chrome.kill('SIGTERM');server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});

/* Isolated browser regression. Serves local assets and a fake API on a random
 * loopback port. No request is routed to a robot or a running workbench. */
const http = require('http'), fs = require('fs'), path = require('path');
const assert = require('assert'), cp = require('child_process'), os = require('os');
const WebSocket = require('ws');
const root = path.resolve(__dirname, '..'), output = process.env.F1_UIUX_BROWSER_OUTPUT || path.join(root, 'results/data-capture-modes-20261029/browser');
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
const catalog=JSON.parse(fs.readFileSync(path.join(root,'results/data-capture-modes-20261029/catalog-candidate.json')));
const server=http.createServer(async(req,res)=>{
 const u=new URL(req.url,'http://127.0.0.1');
 if(req.method!=='GET'){posts.push(req.url);res.statusCode=403;res.end();return;}
 if(process.env.F1_CAPTURE_REVIEW_URL){const target=new URL(req.url,process.env.F1_CAPTURE_REVIEW_URL);const upstream=http.request(target,{method:'GET',headers:{...req.headers,host:'127.0.0.1:4242'}},reply=>{res.writeHead(reply.statusCode,reply.headers);reply.pipe(res);});upstream.on('error',e=>{res.statusCode=502;res.end(String(e));});upstream.end();return;}
 let data;
 if(u.pathname==='/api/robot')data={robot};
 else if(u.pathname==='/api/session')data={authenticated:true};
 else if(u.pathname==='/api/state')data=state();
 else if(u.pathname==='/api/data'||u.pathname==='/api/data/catalog')data=catalog;
 else if(u.pathname.startsWith('/api/data/episodes/'))data={episode:catalog.episodes.find(e=>e.id===u.pathname.split('/').pop())};
 else if(u.pathname==='/api/hil/routes')data={routes:hilRoutes};
 else if(u.pathname==='/api/hil/state')data={hil:hilFixture};
 else if(u.pathname==='/api/teleop/state')data={teleop:teleopFixture};
 else if(u.pathname==='/api/recording/state')data={recording:recorderFixture};
 else if(u.pathname==='/api/presets')data={presets:[]};
 else if(u.pathname==='/api/dev/revision')data={revision:'capture-mode-check'};
 else if(u.pathname.startsWith('/api/'))data={};
 if(data!==undefined){res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));return;}
 if(u.pathname==='/model-viewer.js'){res.setHeader('Content-Type','text/javascript');res.end('export function createRobotViewer(){return {update(){},dispose(){},resize(){}}}');return;}
 const rel=u.pathname==='/'?'index.html':decodeURIComponent(u.pathname).slice(1),web=path.join(root,'web'),file=path.resolve(web,rel);
 if(!file.startsWith(web+'/')||!fs.existsSync(file)){res.statusCode=404;res.end();return;}
 res.setHeader('Content-Type',({'.html':'text/html','.js':'text/javascript','.css':'text/css','.json':'application/json'})[path.extname(file)]||'application/octet-stream');fs.createReadStream(file).pipe(res);
});
const wait=ms=>new Promise(r=>setTimeout(r,ms));
function get(url){return new Promise((resolve,reject)=>http.get(url,r=>{let s='';r.on('data',b=>s+=b);r.on('end',()=>{try{resolve(JSON.parse(s))}catch(e){reject(e)}})}).on('error',reject));}
(async()=>{
 fs.mkdirSync(output,{recursive:true});await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const port=server.address().port,profileDir=fs.mkdtempSync(path.join(os.tmpdir(),'f1-mode-browser-'));
 const chrome=cp.spawn('/usr/bin/google-chrome',['--headless=new','--no-sandbox','--disable-dev-shm-usage','--remote-debugging-port=0','--user-data-dir='+profileDir,'about:blank'],{stdio:'ignore'});
 let ws;
 try{
  const active=path.join(profileDir,'DevToolsActivePort');for(let i=0;i<100&&!fs.existsSync(active);i++)await wait(100);
  const debug=fs.readFileSync(active,'utf8').split('\n')[0],tabs=await get('http://127.0.0.1:'+debug+'/json/list');
  ws=new WebSocket(tabs.find(t=>t.type==='page').webSocketDebuggerUrl);await new Promise((r,j)=>{ws.on('open',r);ws.on('error',j)});
  let id=0;const pending=new Map(),errors=[];
  ws.on('message',raw=>{const p=JSON.parse(raw);if(p.id&&pending.has(p.id)){const h=pending.get(p.id);pending.delete(p.id);p.error?h.reject(Error(JSON.stringify(p.error))):h.resolve(p.result);}else if(p.method==='Runtime.exceptionThrown')errors.push(p.params.exceptionDetails);else if(p.method==='Network.requestIntercepted'){const request=p.params.request;if(request.method!=='GET'){posts.push(request.url);call('Network.continueInterceptedRequest',{interceptionId:p.params.interceptionId,errorReason:'BlockedByClient'});}else call('Network.continueInterceptedRequest',{interceptionId:p.params.interceptionId});}});
  const call=(method,params={})=>new Promise((resolve,reject)=>{const n=++id;pending.set(n,{resolve,reject});ws.send(JSON.stringify({id:n,method,params}));});
  const evaluate=async expression=>{const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
  const until=async(expression)=>{for(let i=0;i<100;i++){if(await evaluate(expression))return;await wait(100);}console.log(await evaluate("document.querySelector('#data-folders')?.innerHTML.slice(0,6000)||document.body.innerText.slice(0,6000)"));throw Error('Timeout '+expression);};
  const click=selector=>evaluate('document.querySelector('+JSON.stringify(selector)+').click()');
  const screenshot=async name=>{const x=await call('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(output,name+'.png'),Buffer.from(x.data,'base64'));};
  await call('Runtime.enable');await call('Page.enable');await call('Network.enable');await call('Network.setRequestInterception',{patterns:[{urlPattern:'*'}]});const checks=[];
  for(const [width,height] of [[1920,1080],[1440,900],[1280,800]]){
   await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
   await call('Page.navigate',{url:'http://127.0.0.1:'+port+'/#data'});
   await until("document.querySelector('#data-version-tree [data-folder=HIL]')");await click('#enter-workbench');
   await click('#data-version-tree [data-folder=""]');
   await until("[...document.querySelectorAll('#data-folders [data-folder]')].map(e=>e.dataset.folder).filter(Boolean).join('|')==='LXL|真机Rollout|HIL'");
   await screenshot(width+'-root');
   await click('#data-version-tree [data-folder=HIL]');
   await until("document.querySelector('#data-folders [data-folder=\"HIL/RLT\"]')");
   const folders=await evaluate("[...document.querySelectorAll('#data-folders [data-folder]')].filter(e=>e.dataset.folder.startsWith('HIL/')).map(e=>e.innerText)");
   assert(folders.some(x=>x.includes('人工示教')&&x.includes('26 条')));assert(folders.some(x=>x.includes('DAgger')&&x.includes('10 条')));assert(folders.some(x=>x.includes('RLT')&&x.includes('9 条')));
   await screenshot(width+'-hil');
   await click('#data-folders [data-folder="HIL/RLT"]');await until("document.querySelector('#data-folders [data-episode]')");
   assert(await evaluate("[...document.querySelectorAll('#data-folders .recording-row-open small')].every(e=>e.innerText.startsWith('RLT ·'))"));
   await click('#data-folders [data-episode]');await until("document.querySelector('#episode-detail .episode-heading p')");
   assert(await evaluate("document.querySelector('#episode-detail .episode-heading p').innerText.startsWith('RLT ·')"));
   const overflow=await evaluate('document.documentElement.scrollWidth>innerWidth');assert(!overflow);
   await screenshot(width+'-detail');checks.push({width,height,folders,overflow});
  }
  assert.equal(errors.length,0,JSON.stringify(errors));assert.equal(posts.length,0);
  const report={checks,exceptions:errors,mutating_requests:posts,isolated:!process.env.F1_CAPTURE_REVIEW_URL};fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
 }finally{if(ws)ws.close();chrome.kill();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1});

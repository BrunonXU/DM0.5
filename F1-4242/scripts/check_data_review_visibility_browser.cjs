/* Isolated browser regression. Serves local assets and a fake API on a random
 * loopback port. No request is routed to a robot or a running workbench. */
const http = require('http'), fs = require('fs'), path = require('path');
const assert = require('assert'), cp = require('child_process'), os = require('os');
const WebSocket = require('ws');
const root = path.resolve(__dirname, '..'), output = process.env.F1_UIUX_BROWSER_OUTPUT || path.join(root, 'results/data-review-visibility-20261029/browser');
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
const audit=JSON.parse(fs.readFileSync(path.join(root,'results/data-review-visibility-20261029/audit-agent.json')));
for(const evidence of audit.rows){let row=catalog.episodes.find(e=>e.id===evidence.id);if(!row){row={...evidence,source:'hil_policy_hil',capture_mode:'rlt',capture_label:'RLT',library_folder:'HIL/RLT',display_timezone:'Asia/Shanghai',replay_url:'/api/data/hil/'+evidence.id+'/timeline',videos:[]};catalog.episodes.unshift(row);}
 row.review={...row.review,notes:evidence.suggested_notes||evidence.review.notes||''};
 const split=evidence.replay_split==='excluded'?'exclude':evidence.replay_split||'pending';
 const confirmation=evidence.confirmation_label||'待审核';
 row.rlt_assessment={schema:'f1_rlt_assessment_v1',source_signature:row.source_signature,source_bound:true,split,quality:split==='pending'?'pending_review':'reviewed',outcome_label:evidence.outcome_label||'结果未确认',confirmation_label:confirmation,confirmation_kind:confirmation==='人工补确认'?'manual_confirmation':'none',usage_label:split==='train'?'训练局部':split==='validation'?'开发验证局部':split==='exclude'?'不入局部训练':'待审核',transition_count:evidence.transition_count,retained_seconds:evidence.retained_seconds,notes_summary:split==='validation'?'曾参与8步预检，当前用作开发验证。':evidence.suggested_notes||'新增录制，待审核。',original_status:'原始日志保留',source_label:'源绑定审核快照',reviewed_at:'2026-10-29T06:00:00Z'};
 if(row.name.includes('c5a0c2bc'))Object.assign(row.rlt_assessment,{local_start_s:18.6204,terminal_s:43.2207,original_end_s:48.6136,terminal_observation_id:1765,excluded_summary:'排除保持、收尾撤离、复位与Home。'});
}
catalog.archives=JSON.parse(fs.readFileSync(path.join(root,'results/data-review-visibility-20261029/data_archives.json'))).records.map(a=>({...a,library_folder:'HIL/DAgger',display_timezone:'Asia/Shanghai',availability_label:'机器人已删除 · 服务器镜像上次核验完整'}));
const cover=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=','base64');
const server=http.createServer(async(req,res)=>{
 const u=new URL(req.url,'http://127.0.0.1');
 if(req.method!=='GET'){posts.push(req.url);res.statusCode=403;res.end();return;}
 if(process.env.F1_CAPTURE_REVIEW_URL){const target=new URL(req.url,process.env.F1_CAPTURE_REVIEW_URL);const upstream=http.request(target,{method:'GET',headers:{...req.headers,host:'127.0.0.1:4242'}},reply=>{res.writeHead(reply.statusCode,reply.headers);reply.pipe(res);});upstream.on('error',e=>{res.statusCode=502;res.end(String(e));});upstream.end();return;}
 if(u.pathname.endsWith('/head.jpg')){res.setHeader('Content-Type','image/png');res.end(cover);return;}
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
  await call('Runtime.enable');await call('Page.enable');await call('Emulation.setTimezoneOverride',{timezoneId:'Asia/Shanghai'});await call('Network.enable');await call('Network.setRequestInterception',{patterns:[{urlPattern:'*'}]});const checks=[];
  for(const [width,height] of [[1920,1080],[1440,900],[1280,800]]){
   await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
   await call('Page.navigate',{url:'http://127.0.0.1:'+port+'/#data'});await until("document.querySelector('#data-version-tree [data-folder=HIL]')");await click('#enter-workbench');
   await click('#data-version-tree [data-folder="HIL/DAgger"]');
   await until("document.querySelector('[data-folder-summary]')?.textContent.includes('当前目录 1 条')");
   if(await evaluate("Boolean(document.querySelector('[data-browse-mode=folders]'))"))await click('[data-browse-mode=folders]');
   assert(await evaluate("document.querySelector('[data-folder-summary]').textContent.includes('本地 10 条')&&document.querySelector('[data-folder-summary]').textContent.includes('子文件夹 9 条')"));
   assert(await evaluate("document.querySelectorAll('.data-recording-table tbody tr').length===1"));
   assert(await evaluate("document.querySelector('[data-server-archives]').textContent.includes('开发验证')&&!document.querySelector('[data-server-archives] [data-episode-replay]')&&!document.querySelector('[data-server-archives] [data-select-episode]')"));
   await screenshot(width+'-dagger-folders');
   await click('[data-browse-mode=all]');await until("document.querySelectorAll('.data-recording-table tbody tr').length===8");
   assert(await evaluate("document.querySelectorAll('.recording-folder-label').length===8"));
   assert(await evaluate("document.querySelector('.recording-list-heading').textContent.includes('10 条')"));
   await screenshot(width+'-dagger-all');
   await click('#data-version-tree [data-folder="HIL/RLT"]');await until("document.querySelector('[data-rlt-summary]')");
   assert(await evaluate("document.querySelector('[data-rlt-summary]').textContent.includes('训练局部 3 条')&&document.querySelector('[data-rlt-summary]').textContent.includes('开发验证 1 条')&&document.querySelector('[data-rlt-summary]').textContent.includes('不入局部训练 5 条')"));
   assert(await evaluate("document.querySelector('.rlt-recording-table').textContent.includes('成功标记来源')&&document.querySelector('.rlt-recording-table').textContent.includes('局部数据用途')"));
   await until("document.querySelector('.recording-file-icon img')?.naturalWidth>0");
   assert(await evaluate("document.querySelectorAll('.rlt-recording-table [data-episode-replay]').length===8"));
   await screenshot(width+'-rlt-list');
   await click('[data-assessment-filter=manual_confirmation]');await until("document.querySelectorAll('.rlt-recording-table tbody tr').length===2");
   const selected=catalog.episodes.find(e=>e.name.includes('c5a0c2bc'));
   await click('[data-note-edit="'+selected.id+'"]');await until("document.querySelector('.data-note-dialog')?.open");
   assert(await evaluate("document.querySelector('[data-note-input]').value.includes('人工辅助成功')"));
   await click('[data-note-close]');await until("!document.querySelector('.data-note-dialog')");
   await click('[data-episode="'+selected.id+'"]');await until("document.querySelector('.rlt-assessment-panel')");
   assert(await evaluate("document.querySelector('.rlt-assessment-panel').textContent.includes('53 转移')&&document.querySelector('.rlt-range').textContent.includes('43.22 s')"));
   assert(await evaluate("document.querySelector('[data-motion-replay-open]')&&document.querySelector('#review-notes')&&document.querySelector('#review-status')"));
   await evaluate("document.querySelector('#review-notes').value='临时草稿：不要覆盖';document.querySelector('#review-notes').dispatchEvent(new Event('input',{bubbles:true}))");
   await click('[data-click=refresh-data]');await wait(300);
   assert(await evaluate("document.querySelector('#review-notes').value==='临时草稿：不要覆盖'"));
   // Discard only browser-local draft; this endpoint remains read-only.
   await click('[data-click=data-review-discard]');await until("document.querySelector('#review-notes')&&document.querySelector('#review-notes').value!=='临时草稿：不要覆盖'");
   await screenshot(width+'-rlt-detail');
   const overflow=await evaluate('document.documentElement.scrollWidth>innerWidth');assert(!overflow);checks.push({width,height,overflow,dagger_local:10,archives:1,reviewed_train:3,reviewed_validation:1});
   await click('[data-click=data-back]');
  }
  // A new unsummarized RLT take stays pending even when its editable note says success.
  const fresh={...catalog.episodes.find(e=>e.capture_mode==='rlt'),id:'ffffffffffffffffffffffff',path:'/fixture/fresh',name:'fresh-rlt',source_signature:'fresh-source',review:{status:'approved',notes:'成功，已训练',revision:'none'}};delete fresh.rlt_assessment;catalog.episodes.unshift(fresh);
  await click('[data-click=refresh-data]');await wait(300);await click('[data-assessment-filter=pending]');
  await until("document.querySelector('[data-episode=\"ffffffffffffffffffffffff\"]')");
  assert(await evaluate("document.querySelector('[data-episode=\"ffffffffffffffffffffffff\"]').closest('tr').textContent.includes('待审核')"));
  assert.equal(errors.length,0,JSON.stringify(errors));assert.equal(posts.length,0);
  const report={checks,exceptions:errors,mutating_requests:posts,isolated:!process.env.F1_CAPTURE_REVIEW_URL};fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
 }finally{if(ws)ws.close();chrome.kill();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1});

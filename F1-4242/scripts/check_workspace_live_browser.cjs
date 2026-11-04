/* Desktop product UI regression. Default: local assets + isolated fixture API.
 * F1_DATA_PRODUCT_URL enables a GET-only proxy to the deployed workbench.
 * No writes or robot actions are permitted in either mode. */
const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const os = require('os');
const cp = require('child_process');
const assert = require('assert');
const WebSocket = require('ws');
const root = path.resolve(__dirname, '..');
const output = process.env.F1_UIUX_BROWSER_OUTPUT || path.join(root, 'results/deployments/20261029-workspace-layout-r1/live-workspace');
const upstream = process.env.F1_DATA_PRODUCT_URL || '';
if(!upstream)throw Error('Live verification requires an explicit upstream URL');
const profile = JSON.parse(fs.readFileSync(path.join(root, 'configs/f1-09.json')));
const labels = {rlt:'RLT', dagger:'DAgger', demonstration:'人工示教', rollout:'真机Rollout', lxl:'LXL'};
const folders = {rlt:'HIL/RLT', dagger:'HIL/DAgger', demonstration:'HIL/人工示教', rollout:'真机Rollout/B1250', lxl:'LXL/积木'};
const modes = ['rollout','rlt','dagger','demonstration','lxl','rlt','dagger','rollout','demonstration','rlt','lxl','dagger','rollout'];
function sourceMode(row) {return Object.prototype.hasOwnProperty.call(labels,row.capture_mode)?row.capture_mode:['lxl','pico_capture','lerobot'].includes(row.source)?'lxl':row.capture_mode;}
const originalRows = modes.map((mode, i) => {
 const id = (i + 1).toString(16).padStart(24, '0');
 const signature = 'fixture-source-' + id;
 const row = {
  id, name:'f1_fixture_' + mode + '_' + id.slice(-4), path:'/fixture/' + mode + '/' + id,
  source:mode === 'lxl' ? 'lxl' : mode === 'demonstration' ? 'hil_demonstration' : 'hil_policy_hil',
  source_signature:signature, capture_mode:mode, capture_label:labels[mode], library_folder:folders[mode],
  started_at:new Date(Date.UTC(2026,9,29,8,0,0) - i * 61 * 1000).toISOString(),
  modified_at:'2026-10-29T09:00:00Z', duration_s:30 + i * 3.1, size_bytes:100000000 + i,
  file_count:10, files:[], topics:[], recording_status:'committed', display_timezone:'Asia/Shanghai',
  preview_url:'/fixture-cover.png', preview_status:'ready', replay_url:'/api/data/hil/' + id + '/timeline', videos:[],
  review:{status:i === 0 ? 'approved' : 'pending', notes:i === 0 ? '成功！内容审核不等于任务结果或训练准入' : i === 1 ? '人工辅助成功；漏按↑，已确认放入并松爪；唯一备注检索词' : '录制备注 ' + i, revision:'fixture-review-' + id},
 };
 if (mode === 'rlt' && i !== 9) row.rlt_assessment = {
  schema:'f1_rlt_assessment_v1', source_signature:signature, source_bound:true, quality:'reviewed',
  split:i === 1 ? 'train' : 'validation', outcome:'assisted_success', outcome_label:'人工辅助成功',
  confirmation_label:i === 1 ? '人工补确认成功（原始标记保留）' : '现场成功标记',
  confirmation_kind:i === 1 ? 'manual_confirmation' : 'event', usage_label:i === 1 ? '训练集（审核快照）' : '开发验证（不入梯度）',
  transition_count:i === 1 ? 53 : 48, retained_seconds:i === 1 ? 23.40 : 23.20,
  local_start_s:10, terminal_s:25, original_end_s:28, original_status:'interrupted',
  notes_summary:'审核快照，不代表实时训练完成', excluded_summary:'排除撤离与复位',
 };
 if (i === 9) row.review.notes = '成功，已训练（仅是未审核备注，不应获得训练标签）';
 return row;
});
const catalog = {
 available:true, episodes:[...originalRows].reverse(),
 organization:{schema:1, revision:'fixture-0', folders:[{id:'@' + 'a'.repeat(32),parent:'HIL/DAgger',name:'历史子夹'}], memberships:{}},
 archives:[{id:'archive-only',name:'服务器独有旧条',library_folder:'HIL/DAgger',started_at:'2026-10-30T12:00:00Z',purpose_label:'开发验证'}],
 summary:{episode_count:originalRows.length},
};
catalog.organization.memberships[originalRows[12].id] = {folder:'@' + 'a'.repeat(32), source_signature:originalRows[12].source_signature};
catalog.episodes.push({...originalRows[0]}); // A duplicate path must not become another recording.
catalog.episodes.push({id:'e'.repeat(24),name:'派生训练导出',path:'/fixture/export',source:'unified_dataset',started_at:'2026-11-01T00:00:00Z',size_bytes:1});
const catalogBefore = JSON.stringify(catalog);
const writes = [], errors = [], requests = [], upstreamRequests = new Set(), upstreamTimings = [], networkTimings = new Map();
const groups = {left_arm:[],right_arm:[],body:[],head:[]};
Object.keys(profile.joint_limits_deg).forEach(n => groups[n.startsWith('arm_l')?'left_arm':n.startsWith('arm_r')?'right_arm':n.startsWith('body')?'body':'head'].push(n));
const robot = {id:'F1-test',label:'隔离浏览器验收',groups,joint_limits_deg:profile.joint_limits_deg,capabilities:{joints:{available:true},gripper:{available:true},base:{available:true}}};
const state = {boot_id:'fixture-boot',connected:true,authenticated:true,command_activity:{ready:true,active:{}},control:{lease_id:null,owner:null},joints:Object.fromEntries(Object.keys(profile.joint_limits_deg).map(n => [n,{position:0,age_s:0.01}])),grippers:{left:{position:20,age_s:0.01},right:{position:20,age_s:0.01}},cameras:{},services:{},capabilities:robot.capabilities};
const cover = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=','base64');
const server = http.createServer(async(req,res) => {
 if (req.method !== 'GET') { writes.push({method:req.method,url:req.url,blocked:'proxy'});res.writeHead(403);res.end('Read-only browser verification');return; }
 requests.push(req.url);
 if (upstream) {
  const target = new URL(req.url,upstream), transport = target.protocol === 'https:' ? https : http, started = Date.now();
  const request = transport.request(target,{method:'GET',headers:{...req.headers,host:process.env.F1_DATA_PRODUCT_HOST || '127.0.0.1:4242'}},reply => {request.firstByteMs=Date.now()-started;res.writeHead(reply.statusCode,reply.headers);reply.pipe(res);});
  request.checkPath=target.pathname;upstreamRequests.add(request);request.on('close',()=>{upstreamRequests.delete(request);upstreamTimings.push({path:target.pathname,first_byte_ms:request.firstByteMs,elapsed_ms:Date.now()-started,status:res.statusCode});});
  res.on('close',()=>request.destroy());request.setTimeout(15000,()=>request.destroy(Error('Read-only proxy upstream timeout')));
  request.on('error',e => {if(!res.headersSent)res.statusCode=502;if(!res.destroyed)res.end(String(e));});request.end();return;
 }
 const u = new URL(req.url,'http://127.0.0.1');
 if (u.pathname === '/fixture-cover.png' || u.pathname.endsWith('/head.jpg')) {res.setHeader('Content-Type','image/png');res.end(cover);return;}
 let data;
 if(u.pathname === '/api/robot') data = {robot};
 else if(u.pathname === '/api/session') data = {authenticated:true};
 else if(u.pathname === '/api/state') data = state;
 else if(u.pathname === '/api/data' || u.pathname === '/api/data/catalog') data = catalog;
 else if(u.pathname.startsWith('/api/data/episodes/')) data = {episode:catalog.episodes.find(e => e.id === u.pathname.split('/').pop())};
 else if(u.pathname === '/api/hil/state') data = {hil:{active:false,phase:'idle',status:{},events:[],summary_fresh:false}};
 else if(u.pathname === '/api/hil/routes') data = {routes:[]};
 else if(u.pathname === '/api/teleop/state') data = {teleop:{phase:'idle',available:true}};
 else if(u.pathname === '/api/recording/state') data = {recording:{mode:'idle',available:true,ready:true,frames:0,elapsed_s:0}};
 else if(u.pathname === '/api/presets') data = {presets:[]};
 else if(u.pathname === '/api/dev/revision') data = {revision:'data-product-browser-fixture'};
 else if(u.pathname.startsWith('/api/')) data = {};
 if(data !== undefined) {res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));return;}
 if(u.pathname === '/model-viewer.js') {res.setHeader('Content-Type','text/javascript');res.end('export function createRobotViewer(){return {update(){},dispose(){},resize(){}}}');return;}
 const rel = u.pathname === '/' ? 'index.html' : decodeURIComponent(u.pathname).slice(1);
 const web = path.resolve(process.env.F1_UIUX_WEB_ROOT || path.join(root,'web')), file = path.resolve(web,rel);
 if(!file.startsWith(web + path.sep) || !fs.existsSync(file)) {res.statusCode=404;res.end();return;}
 res.setHeader('Content-Type',({'.html':'text/html','.js':'text/javascript','.css':'text/css','.json':'application/json','.png':'image/png','.svg':'image/svg+xml'})[path.extname(file)] || 'application/octet-stream');
 fs.createReadStream(file).pipe(res);
});
const wait = ms => new Promise(r => setTimeout(r,ms));
function get(url) {return new Promise((resolve,reject) => {const transport=url.startsWith('https:')?https:http;transport.get(url,r => {let body='';r.on('data',b => body+=b);r.on('end',() => {try{resolve(JSON.parse(body));}catch(e){reject(e);}});}).on('error',reject);});}
function recordings(data) {
 const paths = new Set();
 return data.episodes.filter(e => {const key=e.path || e.id;if(!key || paths.has(key))return false;paths.add(key);return e.source !== 'unified_dataset';})
  .sort((a,b) => (Date.parse(b.started_at || b.modified_at) || -Infinity) - (Date.parse(a.started_at || a.modified_at) || -Infinity) || a.id.localeCompare(b.id));
}
const exprRows = "[...document.querySelectorAll('.data-recording-table tbody tr')].map(tr=>tr.querySelector('[data-episode]')?.dataset.episode).filter(Boolean)";
const navSnapshot = `(()=>{const bar=document.querySelector('.sidebar'),item=document.querySelector('[data-nav=capture]'),b=bar.getBoundingClientRect(),s=getComputedStyle(item);return {text:document.querySelector('#navigation').innerText,width:b.width,x:b.x,font:s.fontFamily,size:s.fontSize,weight:s.fontWeight,padding:s.padding,height:item.getBoundingClientRect().height}})()`;
(async() => {
 fs.mkdirSync(output,{recursive:true});await new Promise(r => server.listen(0,'127.0.0.1',r));
 const origin = 'http://127.0.0.1:' + server.address().port, profileDir = fs.mkdtempSync(path.join(os.tmpdir(),'f1-data-product-browser-'));
 const chrome = cp.spawn(process.env.CHROME_BIN || '/usr/bin/google-chrome',['--headless=new','--no-sandbox','--disable-dev-shm-usage','--enable-unsafe-swiftshader','--remote-debugging-port=0','--user-data-dir='+profileDir,'about:blank'],{stdio:'ignore'});
 let ws, call;const checks=[];
 try {
  const active=path.join(profileDir,'DevToolsActivePort');for(let i=0;i<100&&!fs.existsSync(active);i++)await wait(100);
  assert(fs.existsSync(active),'Chrome did not create a debugging port');
  const debug=fs.readFileSync(active,'utf8').split('\n')[0],tabs=await get('http://127.0.0.1:'+debug+'/json/list');
  ws=new WebSocket(tabs.find(t=>t.type==='page').webSocketDebuggerUrl);await new Promise((r,j)=>{ws.on('open',r);ws.on('error',j);});
  let nextId=0;const pending=new Map();
  call=(method,params={})=>new Promise((resolve,reject)=>{const id=++nextId;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});
  ws.on('message',raw=>{const p=JSON.parse(raw);if(p.id&&pending.has(p.id)){const h=pending.get(p.id);pending.delete(p.id);p.error?h.reject(Error(JSON.stringify(p.error))):h.resolve(p.result);}
   else if(p.method==='Runtime.exceptionThrown')errors.push(p.params.exceptionDetails);
   else if(p.method==='Network.requestWillBeSent'){networkTimings.set(p.params.requestId,{url:p.params.request.url,started:p.params.timestamp});}
   else if(p.method==='Network.responseReceived'){const row=networkTimings.get(p.params.requestId);if(row){row.status=p.params.response.status;row.response_timestamp=p.params.timestamp;row.timing=p.params.response.timing;}}
   else if(p.method==='Network.loadingFailed'){const row=networkTimings.get(p.params.requestId);if(row){row.failed=p.params.errorText;row.canceled=p.params.canceled;row.finished=p.params.timestamp;}}
   else if(p.method==='Network.loadingFinished'){const row=networkTimings.get(p.params.requestId);if(row){row.finished=p.params.timestamp;row.bytes=p.params.encodedDataLength;}}
   else if(p.method==='Network.requestIntercepted'){const request=p.params.request;if(request.method!=='GET'){writes.push({method:request.method,url:request.url,blocked:'browser'});call('Network.continueInterceptedRequest',{interceptionId:p.params.interceptionId,errorReason:'BlockedByClient'});}else call('Network.continueInterceptedRequest',{interceptionId:p.params.interceptionId});}});
  const evaluate=async expression=>{const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
  const until=async(expression,label=expression)=>{for(let i=0;i<100;i++){if(await evaluate(expression))return;await wait(100);}throw Error('Timeout: '+label+'\n'+await evaluate("document.body.innerText.slice(0,5000)"));};
  const click=selector=>evaluate('document.querySelector('+JSON.stringify(selector)+').click()');
  const value=async(selector,text,event='change')=>evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});el.value=${JSON.stringify(text)};el.dispatchEvent(new Event(${JSON.stringify(event)},{bubbles:true}));})()`);
  const screenshot=async name=>{await evaluate('window.scrollTo(0,0)');await wait(50);const r=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(output,name+'.png'),Buffer.from(r.data,'base64'));};
  const settleMedia=async()=>{if(!upstream)return;await wait(200);for(let i=0;i<8;i++){if(![...upstreamRequests].some(r=>/head\.jpg$/.test(r.checkPath||'')))break;await wait(250);}};
  const visibleRows=()=>evaluate(exprRows);
  await call('Runtime.enable');await call('Page.enable');await call('Network.enable');
  await call('Network.setRequestInterception',{patterns:[{urlPattern:'*'}]});
  await call('Emulation.setTimezoneOverride',{timezoneId:'Asia/Shanghai'});
  await call('Page.navigate',{url:origin+'/#overview'});
  await until("document.querySelectorAll('#navigation .nav-item').length===4",'four navigation entries');
  await until("document.querySelector('#entry-notice')?.open",'entry notice');await click('#enter-workbench');
  const routes=[['overview','overview'],['debug','control'],['hil-live','hil?route=demonstration&view=live'],['rlt-command','hil?route=policy&model=rlt400&view=connect'],['pico-setup','pico?view=connect'],['pico-live','pico?view=live'],['data','data']];
  for(const [width,height] of [[1920,1080],[1440,900],[1280,800]]){
   await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
   for(const [name,hash] of routes){
    await evaluate('location.hash='+JSON.stringify(hash));await wait(700);
    await until('document.body.dataset.page==='+JSON.stringify(hash.split('?')[0]),name+' route');
    if(name==='overview'){
     for(const collapsed of [false,true]){
      if(await evaluate("document.body.classList.contains('nav-collapsed')")!==collapsed)await click('#toggle-navigation');
      assert(await evaluate("(()=>{const m=document.querySelector('.workspace>main'),w=document.querySelector('.workspace');return Math.abs(m.getBoundingClientRect().width-Math.min(1840,w.getBoundingClientRect().width))<2&&getComputedStyle(document.querySelector('.page-heading h1')).fontSize==='28px'&&parseFloat(getComputedStyle(document.querySelector('.health-status')).fontSize)>=14&&parseInt(getComputedStyle(document.querySelector('.health-status')).fontWeight)>=600})()"),'overview shares page width and readable bold device state');
     }
     await click('#toggle-navigation');
     await until("document.querySelector('#startup-title').textContent!=='正在读取基础服务'",'startup state');
     assert(await evaluate("!document.querySelector('#base-start-button').hidden"),'Start All must remain visible');
    }
    if(name==='debug')await until("document.querySelector('#robot-model canvas')",'real model loaded');
    if(name==='rlt-command'){
     await until("document.querySelector('#hil-commands pre')?.textContent.includes('start_f1_rlt.cmd')",'installed single-window command');
     assert(await evaluate("document.querySelectorAll('#hil-commands > .hil-step pre').length===1"),'one primary startup command; legacy commands stay in the optional disclosure');
     const h=(await get(origin+'/api/hil/state')).hil;if(!h.active&&h.phase==='idle'){assert(await evaluate("document.querySelector('#hil-overview h2').textContent.includes('未运行')&&!document.querySelector('#hil-persistent-status').textContent.includes('三个终端')&&document.querySelector('[data-hil-request-finish]').textContent.includes('结束运行端')"),'idle missing run id and one-window instructions');}
    }
    if(name==='pico-setup')assert(await evaluate("document.querySelectorAll('.pico-onboarding li').length===3"));
    if(name==='pico-live')assert(await evaluate("!!document.querySelector('[data-pico-recorder-slot=live] #recorder-card')"));
    if(['overview','hil-live','pico-live'].includes(name)){
     await until("document.querySelectorAll('[data-camera] img').length===3 && [...document.querySelectorAll('[data-camera] img')].every(i=>i.naturalWidth>0)",'three real camera frames');
     await until("[...document.querySelectorAll('[data-camera]')].every(e=>{const i=e.querySelector('img'),r=e.getBoundingClientRect(),v=i.getBoundingClientRect();return i.naturalWidth>0&&Math.abs(r.width/r.height-i.naturalWidth/i.naturalHeight)<.01&&Math.abs(r.width-v.width)<1&&Math.abs(r.height-v.height)<1})",'real full-frame geometry without letterboxing');
    }
    if(name==='overview'){
     await evaluate('window.scrollTo(0,0)');
     const geometry=await evaluate(`(()=>{const e=document.querySelector('.workbench-events');return {page:document.documentElement.scrollHeight,height:innerHeight,nested:e.previousElementSibling?.classList.contains('workbench-devices'),shortcuts:document.querySelectorAll('.workbench-entries').length,start:document.querySelector('#base-start-button').getBoundingClientRect().height}})()`);
     assert(geometry.nested&&geometry.shortcuts===0&&geometry.start>=54,'compact overview hierarchy');
    }
    if(name==='data')await until("document.querySelector('#sidebar-context #data-version-tree [data-folder]')&&document.querySelector('.recording-list-heading')",'live catalogue');
    if(name==='data')assert(await evaluate("(()=>{const n=document.querySelector('#sidebar-context'),p=document.querySelector('[data-nav=data]');return n.parentElement.id==='navigation'&&p.nextElementSibling===n&&n.classList.contains('data-subnav')&&n.getBoundingClientRect().top-p.getBoundingClientRect().bottom<=12})()"),'Data directory is nested directly under Data');
    assert(await evaluate('document.documentElement.scrollWidth<=innerWidth'),name+' horizontal overflow '+width);
    assert(await evaluate("document.querySelector('.workspace-help').href==='http://192.0.2.1:4243/?doc=f1-home'"));
    await screenshot(name+'-'+width);
    checks.push({width,height,page:name});
   }
  }
  await click('.storage-popover>summary');assert(await evaluate("document.querySelector('.storage-popover').open&&!!document.querySelector('.storage-bar')"));await screenshot('storage-open');
  const live=await get(origin+'/api/state');
  assert.strictEqual(writes.length,0,'read-only page verification attempted mutation');
  assert.strictEqual(errors.length,0,JSON.stringify(errors));
  const report={result:'passed',mode:'live GET-only proxy',checks,exceptions:errors,mutating_requests:writes,connected:live.connected,control_owner:live.control?.owner||null,request_count:requests.length,limitations:'Read-only deployed UI and assets; no Start All, motion, teleop or recording was started.'};
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
 }finally{if(ws)ws.close();chrome.kill('SIGTERM');for(const req of upstreamRequests)req.destroy();server.closeAllConnections?.();server.close();}
})().catch(e=>{console.error(e);for(const req of upstreamRequests)req.destroy();server.closeAllConnections?.();server.close();process.exitCode=1;});

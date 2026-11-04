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
const output = process.env.F1_UIUX_BROWSER_OUTPUT || path.join(root, 'results/data-product-20261029/browser');
const upstream = process.env.F1_DATA_PRODUCT_URL || '';
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
 const chrome = cp.spawn(process.env.CHROME_BIN || '/usr/bin/google-chrome',['--headless=new','--no-sandbox','--disable-dev-shm-usage','--remote-debugging-port=0','--user-data-dir='+profileDir,'about:blank'],{stdio:'ignore'});
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
  for(const [width,height] of [[1920,1080],[1440,900],[1280,800]]) {
   await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
   await call('Page.navigate',{url:origin+'/#overview'});
   await until("document.querySelector('[data-nav=data]')");
   await evaluate("sessionStorage.clear();if(document.querySelector('#entry-notice')?.open)document.querySelector('#enter-workbench').click()");
   const navBefore=await evaluate(navSnapshot);
   await click('[data-nav=data]');await until("document.querySelector('[data-browse-mode=folders]')&&document.querySelector('#data-version-tree [data-folder=HIL]')");
   if(await evaluate("document.querySelector('[data-browse-mode=folders]').getAttribute('aria-pressed')!=='true'"))await click('[data-browse-mode=folders]');
   await click('#data-version-tree [data-folder=""]');await until("document.querySelector('.folder-grid')&&document.querySelector('.recording-list-heading')");
   const response=await get(origin+'/api/data'),expected=recordings(response);
   assert(expected.length>0,'catalog has no local recordings');
   assert.equal(await evaluate("document.querySelector('.recording-list-heading h3').textContent"),'文件夹','folder-only root should be labeled as folders');
   assert(await evaluate("document.querySelector('.recording-list-heading').textContent.includes("+JSON.stringify(String(expected.length))+ ")"),'root heading must count all contained recordings');
   assert(await evaluate("document.querySelector('.recording-list-heading').getBoundingClientRect().bottom<=document.querySelector('.folder-grid').getBoundingClientRect().top"),'folder heading must precede the folder cards');
   await screenshot(width+'-root-folders');
   await click('#data-version-tree [data-folder="HIL/RLT"]');
   await until("document.querySelector('.data-breadcrumb')?.textContent.includes('RLT')&&document.querySelector('.data-recording-table')");
   const rltIds=new Set(expected.filter(e=>e.capture_mode==='rlt').map(e=>e.id));
   assert((await visibleRows()).every(id=>rltIds.has(id)),'folder view escaped its RLT folder');
   assert.equal(await evaluate("document.querySelectorAll('[data-server-archives]').length"),0,'archive UI still exists');
   assert.equal(await evaluate("document.querySelector('#data-library-view').innerText.includes('服务器归档')"),false,'archive text still visible');
   await screenshot(width+'-rlt-folder');
   await click('[data-browse-mode=time]');
   await until("document.querySelector('[data-browse-mode=time]').getAttribute('aria-pressed')==='true'&&document.querySelector('.data-recording-table')");
   const firstPage=await visibleRows();
   assert.deepStrictEqual(firstPage,expected.slice(0,8).map(e=>e.id),'time mode must show all sources globally, newest first');
   assert.deepStrictEqual(await evaluate(navSnapshot),navBefore,'left navigation changed between overview and data');
   assert.equal(await evaluate("document.querySelectorAll('.data-recording-table thead th').length"),6,'unified table must have six columns');
   assert.equal(await evaluate("document.querySelector('.data-breadcrumb')?.offsetParent!==null&&!!document.querySelector('.data-breadcrumb')"),false,'time view must not imply current-folder scope');
   assert.equal(await evaluate('document.documentElement.scrollWidth>innerWidth'),false,'desktop page horizontal overflow');
   const fonts=await evaluate(`(()=>{const title=document.querySelector('.data-refinements h1'),name=document.querySelector('.recording-row-open strong'),table=document.querySelector('.data-recording-table');return {title:getComputedStyle(title).fontFamily,titleSize:getComputedStyle(title).fontSize,dateWeight:name?getComputedStyle(name).fontWeight:null,body:getComputedStyle(table).fontFamily}})()`);
   assert(!/Georgia|宋体|SimSun/i.test(fonts.title),'data title must use the shared sans-serif family');
   assert(Number.parseFloat(fonts.titleSize)<=34,'data title should remain restrained');
   if(fonts.dateWeight)assert(Number(fonts.dateWeight)<=600,'dates should not be heavy bold');
   await settleMedia();await screenshot(width+'-all-time');
   if(expected.length>8){await click('[data-data-page="2"]');await until(exprRows+'[0]==='+JSON.stringify(expected[8].id));assert.deepStrictEqual(await visibleRows(),expected.slice(8,16).map(e=>e.id),'second page order or count differs');await screenshot(width+'-time-page-2');}
   const source='dagger',sourceExpected=expected.filter(e=>e.capture_mode===source);
   if(sourceExpected.length){await value('[data-data-filter=sourceMode]',source);await until(exprRows+'[0]==='+JSON.stringify(sourceExpected[0].id));assert.deepStrictEqual(await visibleRows(),sourceExpected.slice(0,8).map(e=>e.id),'source filter must include all matching source folders');await screenshot(width+'-dagger-source');}
   await value('[data-data-filter=sourceMode]','');
   await until(exprRows+'[0]==='+JSON.stringify(expected[0].id));
   const withNote=expected.find(e=>e.capture_mode==='rlt'&&e.rlt_assessment&&e.rlt_assessment.split==='train'&&e.review&&e.review.notes&&!e.review.stale);
   assert(withNote,'needs one RLT training row with a note for note/detail preservation');
   const query=upstream?withNote.name:'唯一备注检索词';
   await value('#data-search',query,'input');await until(exprRows+'.includes('+JSON.stringify(withNote.id)+')');
   if(!upstream)assert.deepStrictEqual(await visibleRows(),[withNote.id],'search must find note text globally');
   await settleMedia();
   await click('[data-note-edit="'+withNote.id+'"]');await until("document.querySelector('.data-note-dialog')?.open");
   assert.equal(await evaluate("document.querySelector('[data-note-input]').value"),withNote.review.notes,'note editor lost existing content');
   await screenshot(width+'-note');await click('[data-note-close]');await until("!document.querySelector('.data-note-dialog')");
   if(!upstream||width===1280){
    await click('[data-episode="'+withNote.id+'"]');await until("document.querySelector('.rlt-assessment-panel')&&document.querySelector('#review-notes')");
    assert.equal(await evaluate("document.querySelector('#review-notes').value"),withNote.review.notes);
    assert(await evaluate("!!document.querySelector('[data-motion-replay-open]')"),'robot Replay entry must remain in detail');
    if(!upstream){
     const draft='浏览器本地草稿：刷新后保留，不提交';
     await value('#review-notes',draft,'input');await click('[data-click=refresh-data]');await wait(250);
     assert.equal(await evaluate("document.querySelector('#review-notes').value"),draft,'refresh overwrote an unsaved note');
     await click('[data-click=data-review-discard]');await until("document.querySelector('#review-notes')&&document.querySelector('#review-notes').value!=="+JSON.stringify(draft));
    }
    await screenshot(width+'-rlt-detail');await click('[data-click=data-back]');await until("!document.querySelector('#data-library-view').hidden");
   }
   await value('#data-search','','input');
   await click('[data-browse-mode=folders]');await until("document.querySelector('.data-breadcrumb')?.textContent.includes('RLT')");
   assert((await visibleRows()).every(id=>rltIds.has(id)),'returning to folders must preserve previous RLT folder');
   await click('[data-browse-mode=time]');await until(exprRows+'[0]==='+JSON.stringify(expected[0].id));
   if(!upstream){
    await value('[data-data-filter=sourceMode]','rollout');await until(exprRows+'[0]==='+JSON.stringify(originalRows[0].id));
    const firstText=await evaluate("document.querySelector('.data-recording-table tbody tr').innerText");
    assert(firstText.includes('未标注'),'a review-approved note must not imply task success');
    assert(!firstText.includes('训练可用'),'content approval must not grant training eligibility');
    await value('[data-data-filter=sourceMode]','rlt');await until(exprRows+'.includes('+JSON.stringify(originalRows[9].id)+')');
    assert(await evaluate(`document.querySelector('[data-episode="${originalRows[9].id}"]').closest('tr').innerText.includes('待审核')`),'new unreviewed RLT must stay pending despite note text');
    await value('[data-data-filter=sourceMode]','');
   }
   checks.push({width,height,local_records:expected.length,source_counts:Object.fromEntries(Object.keys(labels).map(mode=>[mode,expected.filter(e=>sourceMode(e)===mode).length])),folder_root_total:true,global_order:true,source_filter:true,note_editor:true,note_draft_preserved:upstream?'passed in isolated regression':true,first_detail_checked:!upstream||width===1280,folder_return:true,navigation_unchanged:true,fonts});
  }
  assert.equal(errors.length,0,JSON.stringify(errors));assert.equal(writes.length,0,JSON.stringify(writes));assert.equal(JSON.stringify(catalog),catalogBefore,'fixture data or archive metadata mutated');
  const report={result:upstream?'passed_with_known_stress_limit':'passed',mode:upstream?'live GET-only proxy':'isolated fixture API',checks,exceptions:errors,mutating_requests:writes,request_count:requests.length,upstream_timings:upstreamTimings,network_timings:[...networkTimings.values()],live_stress_limit:upstream?{passed:false,scope:'Rapid detail refresh/discard while three real video streams preload over SSH forwarding.',observed:'Two earlier live attempts hit the unchanged frontend 5-second API timeout. Routine list/source/note-editor and a single detail open are checked separately; this does not establish video playback or stress reliability.'}:null};
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
 } catch(error) {
  if(call){try{const shot=await call('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(output,'failure.png'),Buffer.from(shot.data,'base64'));}catch{}}
  fs.writeFileSync(path.join(output,'failure.json'),JSON.stringify({error:String(error),stack:error.stack,checks,exceptions:errors,mutating_requests:writes,upstream_timings:upstreamTimings,network_timings:[...networkTimings.values()],pending_upstream:[...upstreamRequests].map(r=>r.checkPath)},null,2));throw error;
 } finally {if(ws)ws.close();chrome.kill('SIGTERM');for(const request of upstreamRequests)request.destroy();server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});

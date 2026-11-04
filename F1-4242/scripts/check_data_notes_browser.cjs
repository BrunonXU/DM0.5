/* Isolated browser regression. Serves local assets and a fake API on a random
 * loopback port. No request is routed to a robot or a running workbench. */
const http = require('http'), fs = require('fs'), path = require('path');
const assert = require('assert'), cp = require('child_process'), os = require('os');
const WebSocket = require('ws');
const root = path.resolve(__dirname, '..'), output = process.env.F1_DATA_BROWSER_OUTPUT || path.join(root, 'results/data-notes-20261026/browser');
const webRoot = process.env.F1_DATA_BROWSER_WEB_ROOT || process.env.F1_REPLAY_BROWSER_WEB_ROOT || path.join(root,'web');
const profile = JSON.parse(fs.readFileSync(path.join(root, 'configs/f1-09.json')));
const groups = {left_arm:[],right_arm:[],body:[],head:[]};
Object.keys(profile.joint_limits_deg).forEach(n => groups[n.startsWith('arm_l')?'left_arm':n.startsWith('arm_r')?'right_arm':n.startsWith('body')?'body':'head'].push(n));
const target = Object.assign({}, ...Object.keys(profile.joint_limits_deg).map(n => ({[n]:0})), {body_j3:-5,body_j4:-10,body_j5:-5});
const robot = {id:'F1-test',label:'模拟机器人',groups:groups,joint_limits_deg:profile.joint_limits_deg,capabilities:{joints:{available:true},gripper:{available:true},base:{available:true}}};
const authenticated=true, lease=null, current=null, posts=[];
let detailDelays=[],detailRequests=0,saveDelay=0,saveFailure=0;
const eid='eeeeeeeeeeeeeeeeeeeeeeee';
const episode={id:eid,name:'f1_hil_20261013T140457Z_2bb86113',path:'/data/hil_data/demonstration/f1_hil_fixture',source:'hil_demonstration',execution:'powered',recording_status:'committed',source_signature:'fixture',size_bytes:479200000,started_at:'2026-10-13T14:04:57Z',files:Array.from({length:80},(_,i)=>({relative_path:'journal/record-'+i+'.jsonl',size_bytes:20000,modified_at:'2026-10-13T14:05:36Z'})),topics:[],videos:[],duration_s:39.1,review:{revision:'none',status:'pending'},replay_url:'/api/data/hil/'+eid+'/timeline'};
episode.duration_s=12;episode.videos=['head','left','right'].map(camera=>({camera,url:'/api/data/video/'+eid+'/'+camera+'.mp4',offset_s:0}));episode.analytics_url='/api/data/analytics/'+eid;
let catalog=Array.from({length:137},(_,i)=>Object.assign({},episode,{id:i===0?eid:i.toString(16).padStart(24,'0'),name:i===0?episode.name:'f1_hil_20261013_take_'+String(i).padStart(3,'0'),path:'/data/hil_data/demonstration/'+i,review:{revision:'none',status:i%2?'approved':'pending'}}));
catalog.push({...episode,id:'a'.repeat(24),name:'RTC HIL',source:'hil_policy_hil',path:'/mock/hil',library_folder:'DAgger/策略 HIL'}, {...episode,id:'b'.repeat(24),name:'Full 1K rollout',source:'hil_policy_hil',path:'/mock/rollout',library_folder:'真机 rollout/Full 1K'}, {...episode,id:'c'.repeat(24),name:'legacy',source:'lxl',path:'/mock/legacy',relative_path:'legacy'});

catalog=catalog.slice(0,5);
catalog.forEach((row,i)=>{row.name='2026-10-26 09:'+String(43-i).padStart(2,'0')+':24 · 执行 46.2秒';row.started_at='2026-10-26T01:'+String(43-i).padStart(2,'0')+':24Z';row.display_timezone='Asia/Shanghai';row.duration_s=44.8+i;row.review={revision:'r'+i,status:i===0?'approved':'pending',reviewer:i===0?'original-reviewer':undefined,updated_at:i===0?'2026-10-23T08:00:00Z':undefined,notes:['V2 · 桶向右移，观察插入和释放','孔口阶段有停顿，后续留意释放时机与撤离轨迹','','暂停恢复测试：留意重新按 RB 后的动作衔接，以及夹爪命令是否重复。\n回看时关注暂停前后。','中途结束，保留片段供后续排查'][i]};});
let noteRevision=0;const coverGets=[];let coverActive=0,coverPeak=0;
catalog.forEach(row=>row.replay_url='/api/data/hil/'+row.id+'/timeline');

function fixtureImage(camera){const wide=camera==='head',w=wide?1280:640,h=wide?720:480;return `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><rect width="${w}" height="${h}" fill="#e8edf4"/><path d="M0 ${h*.67} L${w*.5} ${h*.44} L${w} ${h*.67} V${h} H0" fill="#cfd9e5"/><circle cx="${w*.5}" cy="${h*.46}" r="${h*.12}" fill="#9bb0c5"/><text x="${w*.5}" y="${h*.85}" text-anchor="middle" fill="#566c84" font-family="sans-serif" font-size="24">${camera} · OFFLINE FIXTURE</text></svg>`;}
function state(){return {connected:true,authenticated:authenticated,control:{lease_id:lease,owner:lease?'browser':null},joints:Object.assign({},...Object.keys(target).map(n=>({[n]:{position:target[n],age_s:.01}}))),grippers:{left:{position:20,age_s:.01},right:{position:20,age_s:.01}},cameras:{},services:{},capabilities:robot.capabilities,action:current,system_health:{disk:{total_bytes:1e12,used_bytes:2.5e11,free_bytes:7.5e11}}};}
const server = http.createServer(async (req,res)=>{
 const url = new URL(req.url,'http://127.0.0.1');let raw='';for await(const chunk of req)raw+=chunk;
 const body=raw?JSON.parse(raw):null;if(req.method==='POST')posts.push({path:url.pathname,body:body});
 let data;
 if(/^\/api\/data\/hil\/[a-f0-9]{24}\/head\.jpg$/.test(url.pathname)){
  coverGets.push(req.url);coverPeak=Math.max(coverPeak,++coverActive);await new Promise(r=>setTimeout(r,25));coverActive--;
  if(url.pathname.includes('/'+catalog[4].id+'/')){res.writeHead(404);res.end();return;}
  res.writeHead(200,{'Content-Type':'image/svg+xml'});res.end(fixtureImage('head'));return;
 }
 else if(url.pathname.startsWith('/api/data/video/')){const camera=path.basename(url.pathname).split('.')[0],buf=fs.readFileSync(path.join(root,'results/data-redesign-20261022',camera+'.mp4'));const m=(req.headers.range||'').match(/bytes=(\d+)-(\d*)/);const start=m?Number(m[1]):0,end=m&&m[2]?Math.min(Number(m[2]),buf.length-1):buf.length-1;res.statusCode=m?206:200;res.setHeader('Content-Type','video/mp4');res.setHeader('Accept-Ranges','bytes');res.setHeader('Content-Length',end-start+1);if(m)res.setHeader('Content-Range',`bytes ${start}-${end}/${buf.length}`);res.end(buf.subarray(start,end+1));return;}
 else if(url.pathname.startsWith('/api/data/analytics/'))data={timestamps_s:Array.from({length:360},(_,i)=>i/30),channel_names:[...Object.keys(target).filter(n=>n.startsWith('arm_')),'gripper_left','gripper_right'],units:[...Array(14).fill('deg'),'%','%'],duration_s:12,state:Array.from({length:360},(_,i)=>Array.from({length:16},(_,j)=>Math.sin(i/30+j)*5)),action:Array.from({length:360},(_,i)=>Array.from({length:16},(_,j)=>Math.sin(i/30+j)*5+(j<14?.5:2)))};
 else if(url.pathname==='/api/replay/review')data={review:{executable:false,can_align:false,issues:['缺少现场执行条件（隔离测试）'],command_count:200,duration_s:12,events:[]}};
 else if(url.pathname==='/api/replay/reports')data={reports:[]};
 else if(url.pathname==='/api/robot')data={robot:robot};
 else if(url.pathname==='/api/session')data={authenticated:authenticated};
 else if(url.pathname==='/api/state')data=state();
 else if(url.pathname==='/api/data')data={episodes:catalog,available:true,summary:{episode_count:catalog.length},recording:{state:'no_process_observed'}};
 else if(url.pathname.startsWith('/api/data/episodes/')){detailRequests++;data={episode:JSON.parse(JSON.stringify(catalog.find(e=>e.id===url.pathname.split('/').pop())||episode))};if(detailDelays.length){const delay=detailDelays.shift();data.episode.name='request-'+detailRequests;await new Promise(r=>setTimeout(r,delay));}}
 else if(url.pathname==='/api/data/notes'){
  const row=catalog.find(e=>e.id===body.episode_id);
  const conflict=body.expected_source_signature!==row.source_signature?'SOURCE_CHANGED':body.expected_review_revision!==row.review.revision?'REVIEW_CONFLICT':null;
  if(conflict||saveFailure){res.statusCode=conflict?409:saveFailure;res.setHeader('Content-Type','application/json');res.end(JSON.stringify({error:conflict==='SOURCE_CHANGED'?'来源已变化，请重新读取':conflict?'备注已更新，请核对最新内容':'模拟保存失败',code:conflict||'TEST_FAILURE',review:row.review}));return;}
  row.review={...row.review,notes:body.notes,revision:'note-'+(++noteRevision),notes_updated_at:'2026-10-26T02:00:00Z',notes_editor:'note-editor'};
  data={review:JSON.parse(JSON.stringify(row.review))};if(saveDelay)await new Promise(r=>setTimeout(r,saveDelay));
 }
 else if(url.pathname==='/api/data/reviews'){if(saveFailure){res.statusCode=saveFailure;res.setHeader('Content-Type','application/json');res.end(JSON.stringify({error:'保存失败 '+saveFailure,code:saveFailure===409?'REVIEW_CONFLICT':'TEST_FAILURE',review:{revision:'external',status:'rejected',notes:'external'}}));return;}const row=catalog.find(e=>e.id===body.episode_id);row.review={revision:String(Number(row.review.revision)||0)+Date.now(),status:body.status,notes:body.notes,source_signature:row.source_signature,updated_at:'2026-10-14T09:00:00Z'};data={review:row.review};if(saveDelay)await new Promise(r=>setTimeout(r,saveDelay));}
 else if(url.pathname==='/api/data/delete'){const ids=body.episodes.map(e=>e.id);const deleted=catalog.filter(e=>ids.includes(e.id));catalog=catalog.filter(e=>!ids.includes(e.id));data={complete:true,transaction:'fake-delete',results:deleted.map(e=>({id:e.id,name:e.name,state:'deleted'}))};}
 else if(url.pathname.endsWith('/timeline'))data={duration_s:39.1,frames:1173,observed_hz:30};
 else if(url.pathname.endsWith('.jpg')){res.setHeader('Content-Type','image/svg+xml');res.setHeader('X-F1-Observation-Id','frame-1');res.setHeader('X-F1-Recording-Time',url.searchParams.get('t')||'0');res.end(fixtureImage(url.pathname.split('/').pop().replace('.jpg','')));return;}

 else if(url.pathname==='/api/system')data={system:{version:'test'}};
 else if(url.pathname==='/api/presets')data={presets:[]};
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

  const requests=[];ws.on('message',raw=>{const m=JSON.parse(raw);if(m.method==='Network.requestWillBeSent')requests.push({url:m.params.request.url,method:m.params.request.method});});
  await call('Runtime.enable');await call('Page.enable');await call('Network.enable');await call('Emulation.setLocaleOverride',{locale:'en-GB'});

  const checks=[];
  for(const [width,height] of [[1440,900],[1280,800],[1920,1080]]){
   await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
   await call('Page.navigate',{url:'http://127.0.0.1:'+port+'/#data'});
   await until("document.querySelector('[data-folder=\"DAgger\"]')",'folders');
   if(await evaluate("document.querySelector('#entry-notice')?.open"))await click('#enter-workbench');
   await click('[data-folder=""]');
   const navStyle="(()=>{const read=()=>['.sidebar','.infra-name','.infra-name small','.sidebar .nav-item'].map(q=>{const x=document.querySelector(q),s=getComputedStyle(x);return [s.width,s.fontSize,s.display,s.padding,s.backgroundColor]});const data=read();document.body.dataset.page='overview';const shared=read();document.body.dataset.page='data';return {data,shared}})()";
   const nav=await evaluate(navStyle);assert.deepStrictEqual(nav.data,nav.shared);
   const folderLayout="(()=>{const view=document.querySelector('#data-library-view'),folder=document.querySelector('.folder-sections').getBoundingClientRect(),side=document.querySelector('.data-organization-sidebar').getBoundingClientRect();return !view.classList.contains('data-list-view')&&side.left>=folder.right&&getComputedStyle(document.querySelector('.folder-icon')).filter==='none'&&document.querySelector('#data-storage').innerText.includes('真机 rollout')&&document.querySelector('#data-version-tree').innerText.includes('版本目录')})()";
   await snap('folders-'+width);assert(await evaluate(folderLayout),JSON.stringify(await evaluate("({classes:document.querySelector('#data-library-view').className,filter:getComputedStyle(document.querySelector('.folder-icon')).filter,folder:document.querySelector('.folder-sections').getBoundingClientRect().right,side:document.querySelector('.data-organization-sidebar').getBoundingClientRect().left,storage:document.querySelector('#data-storage').innerText,tree:document.querySelector('#data-version-tree').innerText})")));
   await click('[data-folder="DAgger"]');assert(await evaluate(folderLayout));await snap('parent-folders-'+width);
   await click('[data-folder="DAgger/人工示教"]');
   assert(await evaluate("document.querySelector('#data-library-view').classList.contains('data-list-view')"));
   await until("document.querySelectorAll('[data-note-edit]').length===5",'notes table');
   assert(await evaluate('document.documentElement.scrollWidth<=innerWidth'));
   assert(await evaluate("document.querySelectorAll('.data-recording-table th')[3].textContent==='备注'"));
   assert(await evaluate("!document.querySelector('[data-data-filter=reviewStatus]')"));
   assert(await evaluate("!document.querySelector('.recording-list-heading').textContent.includes('悬停')"));
   const bounds=await evaluate("(()=>{const card=document.querySelector('.data-list-view .data-library'),rect=card.getBoundingClientRect(),fields=Array.from(card.querySelectorAll('.data-filter-bar input,.data-filter-bar select')),heading=card.querySelector('.recording-list-heading').getBoundingClientRect(),table=card.querySelector('table').getBoundingClientRect();return {padding:parseFloat(getComputedStyle(card).paddingLeft),inset:Math.min(heading.left-rect.left,table.left-rect.left,rect.right-table.right),fieldsFit:fields.every(x=>{const b=x.getBoundingClientRect();return b.left>=rect.left+19&&b.right<=rect.right-19&&x.scrollWidth<=x.clientWidth+1}),dateWidths:fields.filter(x=>x.type==='date').map(x=>x.getBoundingClientRect().width)}})()");
   assert(bounds.padding>=20 && bounds.inset>=19 && bounds.fieldsFit && bounds.dateWidths.every(w=>w>=159),JSON.stringify(bounds));
   await until("document.querySelectorAll('[data-cover] img').length===4",'original covers');assert(coverPeak<=2);
   assert(await evaluate("document.querySelectorAll('[data-cover] svg').length===1"));
   const coversBefore=coverGets.length;await evaluate("window.coverNode=document.querySelector('[data-cover] img');window.noteButton=document.querySelector('[data-note-edit]');noteButton.focus()");await click('[data-click=refresh-data]');await wait(300);assert.strictEqual(coverGets.length,coversBefore);assert(await evaluate("document.querySelector('[data-cover] img')===window.coverNode&&document.querySelector('[data-note-edit]')===window.noteButton"));
   await snap('list-'+width);await click('[data-note-edit]');
   await until("document.querySelector('.data-note-dialog[open]')",'editor');await snap('editor-'+width);
   await click('[data-note-close]');await click('[data-episode]');
   await until("document.querySelector('#review-form')",'detail');
   assert(await evaluate("!document.querySelector('.data-review-optional').open"));
   assert(await evaluate("document.querySelector('#review-form h3').textContent==='备注'"));
   assert(await evaluate("!document.querySelector('[data-click=data-save-next]')"));
   assert(await evaluate('document.documentElement.scrollWidth<=innerWidth'));
   await snap('detail-'+width);await click('[data-click=data-back]');
   checks.push({width,height,sharedNavigation:true,yellowFoldersWithRightStorage:true,leafListOnly:true,noOverflow:true,reviewCollapsed:true,noHint:true});
  }
  await call('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false});
  const first='[data-note-edit="'+eid+'"]';
  const setNote=async text=>evaluate('(()=>{const e=document.querySelector("[data-note-input]");e.value='+JSON.stringify(text)+';e.dispatchEvent(new Event("input",{bubbles:true}));})()');
  const submit=()=>evaluate("document.querySelector('.data-note-dialog form').requestSubmit()");
  const closed=()=>until("!document.querySelector('.data-note-dialog[open]')",'saved and closed');
  await click(first);await setNote('新备注\n[8.100s] 持物，不提前松爪');
  await evaluate("window.noteField=document.querySelector('[data-note-input]');noteField.setSelectionRange(4,4)");
  await click('[data-click=refresh-data]');await wait(400);
  assert(await evaluate("document.querySelector('[data-note-input]')===window.noteField && document.activeElement===noteField && noteField.selectionStart===4"));
  await submit();await closed();
  assert.strictEqual(catalog[0].review.status,'approved');assert.strictEqual(catalog[0].review.reviewer,'original-reviewer');
  assert(posts.some(p=>p.path==='/api/data/notes'&&!Object.prototype.hasOwnProperty.call(p.body,'status')));
  // Search includes saved notes. A hidden legacy review filter must not survive.
  await evaluate("{const e=document.querySelector('#data-search');e.value='不提前松爪';e.dispatchEvent(new Event('input',{bubbles:true}));}");
  assert.strictEqual(await evaluate("document.querySelectorAll('[data-note-edit]').length"),1);
  await evaluate("{const e=document.querySelector('#data-search');e.value='';e.dispatchEvent(new Event('input',{bubbles:true}));}");
  await click(first);await setNote('未保存草稿');const postCount=posts.length;await click('[data-note-close]');assert.strictEqual(posts.length,postCount);await click(first);
  assert.strictEqual(await evaluate("document.querySelector('[data-note-input]').value"),'未保存草稿');
  // Retain edits made while a prior snapshot is being saved.
  saveDelay=700;await setNote('保存快照');await submit();await setNote('保存过程中继续写');
  await until("document.querySelector('[data-note-save]')?.textContent==='保存备注' && !document.querySelector('[data-note-save]').disabled",'first save response');
  assert.strictEqual(await evaluate("document.querySelector('[data-note-input]').value"),'保存过程中继续写');saveDelay=0;await submit();await closed();
  assert.strictEqual(catalog[0].review.notes,'保存过程中继续写');
  // Another editor's change is shown, never silently overwritten.
  await click(first);await setNote('我的冲突草稿');catalog[0].review={...catalog[0].review,revision:'external',notes:'另一处的新备注'};
  await submit();await until("!document.querySelector('[data-note-conflict]').hidden",'conflict');
  assert.strictEqual(await evaluate("document.querySelector('[data-note-input]').value"),'我的冲突草稿');
  assert.strictEqual(await evaluate("document.querySelector('[data-note-latest]').textContent"),'另一处的新备注');
  await snap('conflict');await click('[data-note-keep]');await submit();await closed();assert.strictEqual(catalog[0].review.notes,'我的冲突草稿');
  await click(first);await setNote('保留失败草稿');saveFailure=500;await submit();
  await until("document.querySelector('[data-note-error]').textContent.includes('模拟保存失败')",'failure');
  assert.strictEqual(await evaluate("document.querySelector('[data-note-input]').value"),'保留失败草稿');saveFailure=0;await submit();await closed();
  // Changed raw source requires reloading; a stale draft cannot be rebound.
  await click(first);await setNote('旧来源草稿');catalog[0].source_signature='changed-source';catalog[0].review={revision:'source-new',status:'pending',notes:''};
  await submit();await until("!document.querySelector('[data-note-conflict]').hidden",'source conflict');
  assert(await evaluate("document.querySelector('[data-note-keep]').hidden"));
  await click('[data-note-reload]');assert.strictEqual(await evaluate("document.querySelector('[data-note-input]').value"),'');await setNote('新来源备注');await submit();await closed();
  await click('[data-click=refresh-data]');await wait(250);
  // Treat HTML as text in editor, table and tooltip.
  await click(first);await setNote('<img src=x onerror="window.noteXss=1">\n原样显示');await submit();await closed();
  assert(await evaluate("!window.noteXss && !document.querySelector('.data-note-cell img')"));
  await click('[data-episode="'+eid+'"]');await until("document.querySelector('#review-form')",'detail');
  await until("document.querySelector('video[data-camera=head]')?.readyState>=2",'video');
  await click('[data-media-note]');assert(await evaluate("document.querySelector('#review-notes').value.includes('s]')"));
  await evaluate("document.querySelector('#review-form').requestSubmit()");
  await until("document.querySelector('#review-saved').textContent.includes('已保存')",'detail note saved');
  assert.strictEqual(catalog[0].review.status,'pending');
  await click('.data-review-optional summary');await click('[data-review-choice=approved]');await click('[data-click=data-review-commit]');
  await until("document.querySelector('[data-saved-review]').textContent.includes('已通过')",'explicit review');
  assert.strictEqual(catalog[0].review.status,'approved');
  await click('[data-motion-replay-open]');await until("document.querySelector('[data-r-review]').textContent.includes('缺少现场')",'Replay retained');
  await click('[data-video-tab]');assert(!await evaluate("document.querySelector('[data-video-workspace]').hidden"));
  await evaluate("sessionStorage.setItem('f1-uiux:data-filters',JSON.stringify({reviewStatus:'rejected'}))");
  await call('Page.navigate',{url:'http://127.0.0.1:'+port+'/#data'});await call('Page.reload');
  await until("document.querySelectorAll('[data-note-edit]').length===5",'legacy review filter cleared');
  if(await evaluate("document.querySelector('#entry-notice')?.open"))await click('#enter-workbench');
  checks.push({notesOnly:true,reviewIdentityPreserved:true,refreshKeepsInputAndCursor:true,searchNotes:true,cancelNoWrites:true,draftsSurvive:true,inflightEditsPreserved:true,conflictsRequireDecision:true,failuresPreserveDraft:true,sourceChangeCannotRebind:true,htmlEscaped:true,timeNotes:true,reviewExplicit:true,replayRetained:true,hiddenLegacyFilterCleared:true});
  assert.strictEqual(exceptions.length,0,JSON.stringify(exceptions));
  assert(!posts.some(p=>/\/api\/(actions|replay\/(start|align)|control|startup\/start)/.test(p.path)));
  const external=requests.filter(r=>!r.url.startsWith('http://127.0.0.1:')&&!r.url.startsWith('blob:')&&!r.url.startsWith('data:'));assert.strictEqual(external.length,0,JSON.stringify(external.map(r=>r.url.slice(0,160))));
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({checks,exceptions,posts,robotActions:0},null,2));
  console.log(JSON.stringify({checks,exceptions:exceptions.length,output}));
 }finally{if(ws)ws.close();chrome.kill();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1});

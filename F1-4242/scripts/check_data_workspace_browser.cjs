/* Isolated browser regression. Serves local assets and a fake API on a random
 * loopback port. No request is routed to a robot or a running workbench. */
const http = require('http'), fs = require('fs'), path = require('path');
const assert = require('assert'), cp = require('child_process'), os = require('os');
const WebSocket = require('ws');
const root = path.resolve(__dirname, '..'), output = process.env.F1_DATA_BROWSER_OUTPUT || path.join(root, 'results/data_workspace_browser');
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
let catalog=Array.from({length:137},(_,i)=>Object.assign({},episode,{id:i===0?eid:i.toString(16).padStart(24,'0'),name:i===0?episode.name:'f1_hil_20261013_take_'+String(i).padStart(3,'0'),path:'/data/hil_data/demonstration/'+i,review:{revision:'none',status:i%2?'approved':'pending'}}));
function fixtureImage(camera){const wide=camera==='head',w=wide?1280:640,h=wide?720:480;return `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><rect width="${w}" height="${h}" fill="#e8edf4"/><path d="M0 ${h*.67} L${w*.5} ${h*.44} L${w} ${h*.67} V${h} H0" fill="#cfd9e5"/><circle cx="${w*.5}" cy="${h*.46}" r="${h*.12}" fill="#9bb0c5"/><text x="${w*.5}" y="${h*.85}" text-anchor="middle" fill="#566c84" font-family="sans-serif" font-size="24">${camera} · OFFLINE FIXTURE</text></svg>`;}
function state(){return {connected:true,authenticated:authenticated,control:{lease_id:lease,owner:lease?'browser':null},joints:Object.assign({},...Object.keys(target).map(n=>({[n]:{position:target[n],age_s:.01}}))),grippers:{left:{position:20,age_s:.01},right:{position:20,age_s:.01}},cameras:{},services:{},capabilities:robot.capabilities,action:current,system_health:{disk:{total_bytes:1e12,used_bytes:2.5e11,free_bytes:7.5e11}}};}
const server = http.createServer(async (req,res)=>{
 const url = new URL(req.url,'http://127.0.0.1');let raw='';for await(const chunk of req)raw+=chunk;
 const body=raw?JSON.parse(raw):null;if(req.method==='POST')posts.push({path:url.pathname,body:body});
 let data;
 if(url.pathname==='/api/robot')data={robot:robot};
 else if(url.pathname==='/api/session')data={authenticated:authenticated};
 else if(url.pathname==='/api/state')data=state();
 else if(url.pathname==='/api/data')data={episodes:catalog,available:true,summary:{episode_count:catalog.length},recording:{state:'no_process_observed'}};
 else if(url.pathname.startsWith('/api/data/episodes/')){detailRequests++;data={episode:JSON.parse(JSON.stringify(catalog.find(e=>e.id===url.pathname.split('/').pop())||episode))};if(detailDelays.length){const delay=detailDelays.shift();data.episode.name='request-'+detailRequests;await new Promise(r=>setTimeout(r,delay));}}
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
  await call('Runtime.enable');await call('Page.enable');await call('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false});
  await call('Page.navigate',{url:'http://127.0.0.1:'+port+'/#data'});
  await until("document.querySelector('[data-folder=\"DAgger\"]')",'source folders');await click('#enter-workbench');
  await click('[data-folder="DAgger"]');await click('[data-folder="DAgger/人工示教"]');
  await until("document.querySelectorAll('[data-episode]').length===8",'bounded original folder cards');
  assert.strictEqual(await evaluate("document.querySelector('.data-folder-pagination').textContent.includes('137')"),true);
  assert.strictEqual(await evaluate("document.documentElement.scrollWidth<=innerWidth"),true);
  assert.strictEqual(await evaluate("document.querySelectorAll('.data-recording-table tbody tr').length===8"),true);
  await snap('library-desktop');
  assert.strictEqual(await evaluate("!document.querySelector('[data-data-page=\"2\"]').disabled"),true);
  await snap('library-desktop');
  await click('[data-click="data-select-start"]');await click('[data-click="data-select-all"]');
  assert.strictEqual(await evaluate("document.querySelectorAll('[aria-checked=true]').length"),8);
  await click('[data-data-page=\"2\"]');
  assert.strictEqual(await evaluate("document.querySelector('[data-click=\"data-delete\"]').textContent.includes('8')"),true);
  await click('[data-click="data-select-all"]');
  assert.strictEqual(await evaluate("document.querySelector('[data-click=\"data-delete\"]').textContent.includes('16')"),true);
  assert.strictEqual(posts.length,0);
  await click('[data-click="data-delete"]');
  assert.strictEqual(await evaluate("!!document.querySelector('.data-confirm[open]')"),false);
  await until("document.querySelector('.data-folder-pagination').textContent.includes('121')",'delete refresh');
  const deletion=posts.find(p=>p.path==='/api/data/delete');assert.strictEqual(deletion.body.episodes.length,16);
  assert.strictEqual(deletion.body.episodes.every(e=>e.source_signature==='fixture'),true);
  await evaluate("const input=document.querySelector('#data-search');input.focus();input.value='take_13';input.dispatchEvent(new Event('input',{bubbles:true}))");
  assert.strictEqual(await evaluate("document.activeElement.id"),'data-search');
  assert.strictEqual(await evaluate("document.querySelectorAll('[data-episode]').length"),7);
  await click('[data-episode]');await until("document.querySelector('.episode-files')",'detail');
  assert.strictEqual(await evaluate("document.querySelector('#data-library-view').hidden"),true);
  assert.strictEqual(await evaluate("document.querySelector('.data-technical').open"),false);
  assert.strictEqual(await evaluate("document.querySelector('.data-technical').open"),false);
  assert.strictEqual(await evaluate("document.querySelectorAll('[data-hil-replay]').length"),0);
  await evaluate("document.querySelectorAll('.toast').forEach(t=>t.remove())");
  await snap('detail-desktop');
  await evaluate("window.reviewMediaRoot=document.querySelector('[data-video-pending]')");await click('[data-click=refresh-data]');await wait(200);
  assert.strictEqual(await evaluate("document.querySelector('[data-video-pending]')===window.reviewMediaRoot"),true);
  await evaluate("document.querySelector('.data-technical').open=true");
  assert.strictEqual(await evaluate("document.querySelector('.episode-files-scroll').clientHeight<=320"),true);
  assert.strictEqual(await evaluate("document.querySelector('.episode-files-scroll').scrollHeight>320"),true);
  await evaluate("document.querySelector('.data-technical').open=false;document.querySelector('#review-notes').value='检查完成';document.querySelector('#review-notes').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#review-status').value='approved';document.querySelector('#review-status').dispatchEvent(new Event('change',{bubbles:true}))");
  assert.strictEqual(await evaluate("document.querySelector('[data-saved-review]').textContent.includes('待审核')"),true);
  await evaluate("document.querySelector('[data-click=data-review-commit]').click()");
  await until("document.querySelector('#review-saved').textContent.includes('2026')",'save review');
  assert.strictEqual(posts.find(p=>p.path==='/api/data/reviews').body.expected_source_signature,'fixture');
  assert.strictEqual(await evaluate("document.querySelector('[data-saved-review]').textContent.includes('已通过')"),true);
  await call('Emulation.setDeviceMetricsOverride',{width:1280,height:800,deviceScaleFactor:1,mobile:false});await wait(250);
  assert.strictEqual(await evaluate("document.documentElement.scrollWidth<=innerWidth"),true);
  await evaluate("document.querySelectorAll('.toast').forEach(t=>t.remove());window.scrollTo(0,0)");
  await snap('detail-1280');
  await click('[data-click="data-back"]');
  assert.strictEqual(await evaluate("document.querySelector('#data-search').value"),'take_13');
  assert.strictEqual(await evaluate("document.documentElement.scrollWidth<=innerWidth"),true);
  assert.strictEqual(await evaluate("document.querySelector('#data-search').getBoundingClientRect().width>200"),true);
  assert.strictEqual(await evaluate("document.querySelector('[data-episode]').getBoundingClientRect().width>110"),true);
  assert.strictEqual(await evaluate("document.querySelector('#data-storage').getBoundingClientRect().width>200"),true);
  await snap('library-1280');
  // Rebuilding the data page restores an unsaved review, including its source.
  await click('[data-episode]');await until("document.querySelector('#review-notes')",'review before navigation');
  await evaluate("document.querySelector('#review-notes').value='draft stays';document.querySelector('#review-notes').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#review-status').value='rejected';document.querySelector('#review-status').dispatchEvent(new Event('change',{bubbles:true}))");
  await click('[data-nav=overview]');await until("!document.querySelector('#episode-detail')",'left data');
  await click('[data-nav=data]');await until("document.querySelector('#review-notes')",'restored detail');
  assert.strictEqual(await evaluate("document.querySelector('#review-notes').value"),'draft stays');
  assert.strictEqual(await evaluate("document.querySelector('#review-status').value"),'rejected');
  assert.strictEqual(await evaluate("document.querySelector('#review-saved').textContent"),'未保存');
  assert.strictEqual(await evaluate("document.querySelector('[data-saved-review]').textContent.includes('已通过')"),true);
  // A save response can arrive while its form is absent, without losing refresh.
  saveDelay=600;
  const savesBefore=posts.filter(p=>p.path==='/api/data/reviews').length;
  await evaluate("document.querySelector('[data-click=data-review-commit]').click()");
  await click('[data-nav=overview]');await until("!document.querySelector('#episode-detail')",'leave saving');
  await click('[data-nav=data]');await until("document.querySelector('#review-notes')",'saving detail restored');
  assert.strictEqual(await evaluate("document.querySelector('#review-form button[type=submit]').disabled"),true);
  await evaluate("document.querySelector('[data-click=data-review-commit]').click()");await wait(50);
  assert.strictEqual(posts.filter(p=>p.path==='/api/data/reviews').length,savesBefore+1);
  await click('[data-nav=overview]');await wait(750);
  await click('[data-nav=data]');await until("document.querySelector('#review-notes')",'saved detail restored');
  saveDelay=300;
  assert.strictEqual(await evaluate("document.querySelector('#review-notes').value"),'draft stays');
  assert.strictEqual(await evaluate("document.querySelector('#review-saved').textContent.includes('未保存')"),false);
  // Editing during an in-flight save leaves the newer text dirty and recoverable.
  await evaluate("document.querySelector('#review-notes').value='submitted text';document.querySelector('#review-notes').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('[data-click=data-review-commit]').click();document.querySelector('#review-notes').value='newer text';document.querySelector('#review-notes').dispatchEvent(new Event('input',{bubbles:true}))");
  await wait(450);
  assert.strictEqual(await evaluate("document.querySelector('#review-notes').value"),'newer text');
  assert.strictEqual(await evaluate("document.querySelector('#review-saved').textContent"),'未保存');
  await click('[data-nav=overview]');await until("!document.querySelector('#episode-detail')",'leave newer draft');
  await click('[data-nav=data]');await until("document.querySelector('#review-notes')",'newer draft returned');
  assert.strictEqual(await evaluate("document.querySelector('#review-notes').value"),'newer text');
  // Same-ID requests finish out of order after leaving and re-entering the page.
  await click('[data-click=data-back]');detailDelays=[800,30];const beforeDetails=detailRequests;
  await click('[data-episode]');await until("document.querySelector('#episode-detail').textContent.includes('正在打开')",'slow detail');
  await click('[data-nav=overview]');await until("!document.querySelector('#episode-detail')",'leave pending detail');
  await click('[data-nav=data]');await until("document.querySelector('.episode-heading h2')",'retried detail');
  assert.strictEqual(detailRequests,beforeDetails+2);
  const currentName=await evaluate("document.querySelector('.episode-heading').dataset.recordingName");
  assert.strictEqual(currentName,'request-'+detailRequests);await wait(900);
  assert.strictEqual(await evaluate("document.querySelector('.episode-heading').dataset.recordingName"),currentName);
  assert.strictEqual(await evaluate("document.querySelector('#data-library-view').hidden && !!document.querySelector('[data-click=data-back]')"),true);
  // Source-bound drafts require an explicit discard and fresh read before re-review.
  const changedId=await evaluate("new URLSearchParams(location.hash.split('?')[1]).get('episode')");
  await evaluate("document.querySelector('#review-notes').value='old source draft';document.querySelector('#review-notes').dispatchEvent(new Event('input',{bubbles:true}))");
  catalog.find(e=>e.id===changedId).source_signature='new-source';
  await click('[data-click=data-back]');await click('[data-episode="'+changedId+'"]');await until("document.querySelector('#review-notes')",'changed source');
  assert.strictEqual(await evaluate("/来源.*变化|来源已更新/.test(document.querySelector('#review-form').textContent)"),true);
  await click('[data-click=data-review-discard]');await until("document.querySelector('#review-notes')",'fresh source review');
  assert.strictEqual(await evaluate("document.querySelector('#review-notes').value==='old source draft'"),false);
  assert.strictEqual(await evaluate("/来源.*变化|来源已更新/.test(document.querySelector('#review-form').textContent)"),false);
  saveDelay=0;
  await evaluate("document.querySelector('#review-notes').value='new source review';document.querySelector('#review-notes').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('[data-click=data-review-commit]').click()");
  await until("document.querySelector('#review-saved').textContent.includes('已保存')",'new source saved');
  assert.strictEqual(posts.filter(p=>p.path==='/api/data/reviews').slice(-1)[0].body.expected_source_signature,'new-source');
  // A delayed success from the old source cannot approve a newly changed source.
  saveDelay=600;
  await evaluate("document.querySelector('#review-status').value='approved';document.querySelector('#review-status').dispatchEvent(new Event('change',{bubbles:true}));document.querySelector('#review-notes').value='submitted for prior source';document.querySelector('#review-notes').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('[data-click=data-review-commit]').click()");
  await wait(50);const changedRow=catalog.find(e=>e.id===changedId);changedRow.source_signature='third-source';changedRow.review={status:'pending',stale:true};
  await click('[data-click=data-back]');await click('[data-episode="'+changedId+'"]');await until("document.querySelector('#review-notes')",'new source during save');
  await wait(750);
  assert.strictEqual(await evaluate("document.querySelector('#review-saved').textContent"),'未保存');
  assert.strictEqual(await evaluate("/来源.*变化|来源已更新/.test(document.querySelector('#review-form').textContent)"),true);
  assert.strictEqual(await evaluate("document.querySelector('[data-click=data-review-discard]').hidden"),false);
  assert.strictEqual(await evaluate("document.querySelector('[data-saved-review]').textContent.includes('待审核')"),true);
  // A queue remains fixed when background recordings arrive; saving never races editing or navigation.
  saveDelay=0;
  await click('[data-click=data-review-discard]');await until("document.querySelector('#review-notes')",'discard stale source');
  await click('[data-click=data-back]');
  await evaluate("document.querySelector('#data-search').value='take_13';document.querySelector('#data-search').dispatchEvent(new Event('input',{bubbles:true}))");
  await click('[data-episode]');await until("document.querySelector('#review-notes')",'queue first');
  const queueFirst=await evaluate('location.hash');
  await evaluate("document.querySelector('#review-notes').value='queue saved';document.querySelector('#review-notes').dispatchEvent(new Event('input',{bubbles:true}))");
  await click('[data-click=data-review-commit]');await until("document.querySelector('#review-saved').textContent.includes('已保存')",'explicit review saved');await click('[data-click=data-next]');await until('location.hash!=='+JSON.stringify(queueFirst),'explicit next');
  await until("document.querySelector('#review-notes')",'next detail ready');
  const queueSecond=await evaluate('location.hash');
  saveDelay=350;
  await click('[data-click=data-review-commit]');await wait(80);
  await evaluate("document.querySelector('#review-notes').value='new edit while saving';document.querySelector('#review-notes').dispatchEvent(new Event('input',{bubbles:true}))");await wait(500);
  assert.strictEqual(await evaluate('location.hash'),queueSecond);
  assert.strictEqual(await evaluate("document.querySelector('#review-notes').value"),'new edit while saving');
  saveDelay=0;saveFailure=409;
  await click('[data-click=data-review-commit]');await until("document.querySelector('#review-saved').textContent.includes('409')",'conflict retained');
  assert.strictEqual(await evaluate('location.hash'),queueSecond);
  assert.strictEqual(await evaluate("document.querySelector('#review-notes').value"),'new edit while saving');
  assert.strictEqual(await evaluate("!!document.querySelector('[data-click=data-review-reconcile]')"),true);
  saveFailure=504;
  await click('[data-click=data-review-commit]');await until("document.querySelector('#review-saved').textContent.includes('504')",'timeout retained');
  assert.strictEqual(await evaluate('location.hash'),queueSecond);saveFailure=0;
  await click('[data-click=data-back]');
  assert.strictEqual(await evaluate("!document.querySelector('[data-data-filter=reviewStatus]')"),true);
  await evaluate("const dateFilter=document.querySelector('[data-data-filter=dateFrom]');dateFilter.value='2027-01-01';dateFilter.dispatchEvent(new Event('change',{bubbles:true}))");
  assert.strictEqual(await evaluate("document.querySelectorAll('[data-episode]').length"),0);
  await snap('empty-filter');
  await evaluate("document.querySelectorAll('[data-data-filter=reviewStatus],[data-data-filter=dateFrom]').forEach(el=>{el.value='';el.dispatchEvent(new Event('change',{bubbles:true}));})");
  for(const width of [1920,1440,1280]){
   await call('Emulation.setDeviceMetricsOverride',{width:width,height:width===1920?1080:width===1440?900:width===1280?800:844,deviceScaleFactor:1,mobile:width<600});await wait(100);
   assert.strictEqual(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);await snap('library-'+width);
  }

  const videoRow=catalog.find(e=>e.name.includes('take_13'));videoRow.videos=[{camera:'head',url:'/api/data/video/'+videoRow.id+'/head.mp4'}];
  await evaluate('location.hash='+JSON.stringify('#data?episode='+videoRow.id));await until("document.querySelector('[data-media-time-input]')",'video seek');
  await evaluate("document.querySelector('[data-media-time-input]').value='8.5';document.querySelector('[data-media-time-input]').dispatchEvent(new Event('change',{bubbles:true}))");
  await click('[data-media-note]');assert.strictEqual(await evaluate("document.querySelector('#review-notes').value.includes('[视频 8.500s]')"),true);
  await click('[data-click=data-back]');await evaluate('history.back()');await until("document.querySelector('[data-media-time-input]')",'video restored');
  assert.strictEqual(await evaluate("Number(document.querySelector('[data-media-time-input]').value)"),8.5);
  assert.strictEqual(await evaluate("document.querySelector('[data-media-play]').textContent"),'播放');
  assert.deepStrictEqual(exceptions,[]);
  assert.strictEqual(posts.some(p=>/replay|actions|control/.test(p.path)),false);
  const report={result:'passed',source:'isolated fake API; approved recording list UI; no robot connection',checks:['137 recordings use table rows, 8 per page','source navigation','page-scoped select all','selection survives pagination','single-click fake deletion uses source signatures','search focus survives update','focused detail and return','files and paths collapsed','files scroll bounded to 320px','raw-image player absent from default detail','eight recording rows with pagination', 'storage permanently visible beside folders','review saved with source signature', 'review badge uses saved status, preserves drafts and rejects old-source responses','desktop three sizes no overflow','no control or motion requests','catalog refresh preserves media node','unsaved review survives navigation','save response after leaving data','newer edits survive in-flight save','same-ID out-of-order detail requests','review saving blocks concurrent submits after navigation','changed source draft explicitly discarded and re-read','old save response cannot approve a changed source','fixed queue explicit next and in-flight editing guard','409 and timeout preserve draft and location','hidden review filter removed; date filter and empty results','independent video time entry, note and paused restoration','no browser exceptions']};
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
 }finally{if(ws)ws.close();chrome.kill('SIGTERM');server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});

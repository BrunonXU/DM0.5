/* Isolated browser regression. Serves local assets and a fake API on a random
 * loopback port. No request is routed to a robot or a running workbench. */
const http = require('http'), fs = require('fs'), path = require('path');
const assert = require('assert'), cp = require('child_process'), os = require('os');
const WebSocket = require('ws');
const root = path.resolve(__dirname, '..'), output = process.env.F1_DATA_BROWSER_OUTPUT || path.join(root, 'results/data-approved-20261022/browser');
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
function fixtureImage(camera){const wide=camera==='head',w=wide?1280:640,h=wide?720:480;return `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><rect width="${w}" height="${h}" fill="#e8edf4"/><path d="M0 ${h*.67} L${w*.5} ${h*.44} L${w} ${h*.67} V${h} H0" fill="#cfd9e5"/><circle cx="${w*.5}" cy="${h*.46}" r="${h*.12}" fill="#9bb0c5"/><text x="${w*.5}" y="${h*.85}" text-anchor="middle" fill="#566c84" font-family="sans-serif" font-size="24">${camera} · OFFLINE FIXTURE</text></svg>`;}
function state(){return {connected:true,authenticated:authenticated,control:{lease_id:lease,owner:lease?'browser':null},joints:Object.assign({},...Object.keys(target).map(n=>({[n]:{position:target[n],age_s:.01}}))),grippers:{left:{position:20,age_s:.01},right:{position:20,age_s:.01}},cameras:{},services:{},capabilities:robot.capabilities,action:current,system_health:{disk:{total_bytes:1e12,used_bytes:2.5e11,free_bytes:7.5e11}}};}
const server = http.createServer(async (req,res)=>{
 const url = new URL(req.url,'http://127.0.0.1');let raw='';for await(const chunk of req)raw+=chunk;
 const body=raw?JSON.parse(raw):null;if(req.method==='POST')posts.push({path:url.pathname,body:body});
 let data;
 if(url.pathname.startsWith('/api/data/video/')){const camera=path.basename(url.pathname).split('.')[0],buf=fs.readFileSync(path.join(root,'results/data-redesign-20261022',camera+'.mp4'));const m=(req.headers.range||'').match(/bytes=(\d+)-(\d*)/);const start=m?Number(m[1]):0,end=m&&m[2]?Math.min(Number(m[2]),buf.length-1):buf.length-1;res.statusCode=m?206:200;res.setHeader('Content-Type','video/mp4');res.setHeader('Accept-Ranges','bytes');res.setHeader('Content-Length',end-start+1);if(m)res.setHeader('Content-Range',`bytes ${start}-${end}/${buf.length}`);res.end(buf.subarray(start,end+1));return;}
 else if(url.pathname.startsWith('/api/data/analytics/'))data={timestamps_s:Array.from({length:360},(_,i)=>i/30),channel_names:[...Object.keys(target).filter(n=>n.startsWith('arm_')),'gripper_left','gripper_right'],units:[...Array(14).fill('deg'),'%','%'],duration_s:12,state:Array.from({length:360},(_,i)=>Array.from({length:16},(_,j)=>Math.sin(i/30+j)*5)),action:Array.from({length:360},(_,i)=>Array.from({length:16},(_,j)=>Math.sin(i/30+j)*5+(j<14?.5:2)))};
 else if(url.pathname==='/api/replay/review')data={review:{executable:false,can_align:false,issues:['缺少现场执行条件（隔离测试）'],command_count:200,duration_s:12,events:[]}};
 else if(url.pathname==='/api/replay/reports')data={reports:[]};
 else if(url.pathname==='/api/robot')data={robot:robot};
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

  const requests=[];ws.on('message',raw=>{const m=JSON.parse(raw);if(m.method==='Network.requestWillBeSent')requests.push({url:m.params.request.url,method:m.params.request.method});});
  await call('Runtime.enable');await call('Page.enable');await call('Network.enable');
  const checks=[];
  for(const [width,height] of [[1920,1080],[1440,900],[1280,800]]){
   await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});await call('Page.navigate',{url:'http://127.0.0.1:'+port+'/#data'});
   await until("document.querySelector('#enter-workbench')",'entry');await click('#enter-workbench');await until("document.querySelector('[data-folder=\"DAgger\"]')",'folders');
   await click('[data-folder="DAgger"]');await click('[data-folder="DAgger/人工示教"]');
   await until("document.querySelectorAll('.data-recording-table [data-episode]').length===8",'recording table');
   assert(await evaluate('document.documentElement.scrollWidth<=innerWidth'));
   assert.strictEqual(await evaluate("document.querySelectorAll('#data-folders .folder-item[data-episode]').length"),0);
   await snap('list-'+width);await click('[data-episode]');
   await until("document.querySelector('video[data-camera=head]')?.readyState>=2",'real MP4');await until("document.querySelectorAll('[data-joint-channel]').length===14",'all joint overview');
   assert(await evaluate('document.documentElement.scrollWidth<=innerWidth'));assert.strictEqual(await evaluate("document.querySelectorAll('[data-hil-replay]').length"),0);
   await snap('detail-'+width);await click('[data-click=data-back]');await click('[data-folder=""]');await snap('folders-'+width);checks.push({width,height,leafTable:true,jointCount:14,overflow:false});
  }
  await call('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false});await click('[data-folder="DAgger"]');await click('[data-folder="DAgger/人工示教"]');await click('[data-episode]');await until("document.querySelector('video[data-camera=head]')?.readyState>=2",'video');
  await evaluate("window.seekEvents=0;document.querySelectorAll('video').forEach(v=>v.addEventListener('seeking',()=>window.seekEvents++))");await click('[data-media-play]');const a=await evaluate("document.querySelector('video[data-camera=head]').currentTime");await wait(1800);const b=await evaluate("document.querySelector('video[data-camera=head]').currentTime");assert(b>a+1);
  // A slow stream must freeze the whole group, regardless of which camera it is.
  for(const camera of ['right','head']){
   await evaluate("window.blockedVideo=document.querySelector('video[data-camera="+camera+"]');Object.defineProperty(blockedVideo,'readyState',{configurable:true,get:()=>1});blockedVideo.dispatchEvent(new Event('waiting'))");
   await wait(200);const frozen=Number(await evaluate("document.querySelector('[data-media-time-input]').value"));await wait(650);
   assert(await evaluate("[...document.querySelectorAll('video')].every(v=>v.paused)"),'one stalled stream pauses every camera');
   assert(Math.abs(Number(await evaluate("document.querySelector('[data-media-time-input]').value"))-frozen)<.04,'shared clock freezes');
   await evaluate("delete blockedVideo.readyState;blockedVideo.dispatchEvent(new Event('canplay'))");
   await until("[...document.querySelectorAll('video')].every(v=>!v.paused)",'group resumes');await wait(450);
  }
  assert(await evaluate('window.seekEvents<20'),'no seek storm');await click('[data-media-play]');
  const seeksBefore=await evaluate('window.seekEvents');
  await evaluate("const slider=document.querySelector('[data-media-seek]');for(let i=0;i<30;i++){slider.value=String(2+i/10);slider.dispatchEvent(new Event('input',{bubbles:true}));}");await wait(350);
  assert(await evaluate('window.seekEvents')-seeksBefore<=3,'drag inputs coalesce to one seek per camera');
  assert(Math.abs(await evaluate("document.querySelector('video[data-camera=head]').currentTime")-4.9)<.1,'final drag target used');
  await evaluate("const e=document.querySelector('[data-media-time-input]');e.value='8.1';e.dispatchEvent(new Event('change',{bubbles:true}))");await wait(250);assert(Math.abs(await evaluate("document.querySelector('video[data-camera=head]').currentTime")-8.1)<.1);
  await click('[data-media-note]');assert(await evaluate("document.querySelector('#review-notes').value.includes('8.100s')"));await click('[data-note-seek]');await click('[data-joint-channel="7"]');assert(await evaluate("document.querySelector('[data-curve-details]').open"));await snap('joint-curve');
  assert.strictEqual(await evaluate("document.querySelector('.joint-overview-kpis strong').textContent"),'0.50°');
  await click('[data-review-choice=approved]');await evaluate("document.querySelector('[data-click=data-review-commit]').click()");await until("document.querySelector('#review-saved').textContent.includes('已保存')",'saved');assert(posts.some(p=>p.path==='/api/data/reviews'&&p.body.status==='approved'));
  await click('[data-media-focus]');assert(await evaluate("document.querySelector('.data-approved-detail').classList.contains('data-video-focus')"));await click('[data-media-focus]');
  await click('.data-technical summary');assert(await evaluate("document.querySelector('.data-technical').open"));await snap('technical');
  await click('[data-motion-replay-open]');await until("document.querySelector('[data-r-review]').textContent.includes('缺少现场')",'replay reason');assert(await evaluate("document.querySelector('[data-video-workspace]').hidden"));await snap('replay');await click('[data-video-tab]');assert(!await evaluate("document.querySelector('[data-video-workspace]').hidden"));
  for(const eid of ['a'.repeat(24),'b'.repeat(24),'c'.repeat(24)]){await call('Page.navigate',{url:'http://127.0.0.1:'+port+'/#data?episode='+eid});await until("document.querySelector('#episode-detail')?.dataset.episodeSignature?.includes('"+eid+"') && document.querySelector('[data-motion-replay-open]')",'source Replay');if(await evaluate("document.querySelector('#entry-notice')?.open"))await click('#enter-workbench');await click('[data-motion-replay-open]');await until("document.querySelector('[data-r-review]').textContent.includes('缺少现场')",'source checks');}
  const currentEpisode=catalog.find(e=>e.id==='c'.repeat(24));currentEpisode.videos=[];delete currentEpisode.analytics_url;currentEpisode.preview_status='waiting_server';await call('Page.navigate',{url:'http://127.0.0.1:'+port+'/#data?episode='+currentEpisode.id});await call('Page.reload');await until("document.querySelector('[data-video-pending]')",'pending');if(await evaluate("document.querySelector('#entry-notice')?.open"))await click('#enter-workbench');await snap('pending');
  const mod=await evaluate("import('/data-presentation.js').then(({jointMetrics})=>jointMetrics({channel_names:['arm_l_j1','arm_r_j1','gripper_right'],units:['deg','deg','%'],timestamps_s:[0,1,2],state:[[0,0,1],[0,null,1],[0,null,1]],action:[[1,3,4],[-1,9,4],[1,9,4]]}))");assert.strictEqual(mod.mean,2);assert.strictEqual(mod.count,2);assert.strictEqual(mod.channels[2].mean,3);
  checks.push({nativeVideo:true,slowWristPausesGroup:true,slowHeadPausesGroup:true,groupRecoversTogether:true,sharedTimelineFreezes:true,dragCoalesced:true,noSeekStorm:true,seek:true,notes:true,reviewSave:true,allOriginsReplay:true,metricsEqualWeightAndMissing:true,pending:true});
  assert.strictEqual(exceptions.length,0,JSON.stringify(exceptions));assert(!requests.some(r=>/\/api\/data\/hil\/.*(timeline|jpg)/.test(r.url)));assert(!posts.some(p=>/\/api\/(actions|replay\/(start|align)|control)/.test(p.path)));
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({checks,exceptions,posts,rawImageRequests:0},null,2));console.log(JSON.stringify({checks,exceptions:exceptions.length}));
 }finally{if(ws)ws.close();chrome.kill();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1});

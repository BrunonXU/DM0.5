/* Isolated browser regression. Serves local assets and a fake API on a random
 * loopback port. No request is routed to a robot or a running workbench. */
const http = require('http'), fs = require('fs'), path = require('path');
const assert = require('assert'), cp = require('child_process'), os = require('os');
const WebSocket = require('ws');
const root = path.resolve(__dirname, '..'), output = process.env.F1_DATA_BROWSER_OUTPUT || path.join(root, 'results/data_workspace_browser');
const webRoot = process.env.F1_REPLAY_BROWSER_WEB_ROOT || path.join(root,'web');
const profile = JSON.parse(fs.readFileSync(path.join(root, 'configs/f1-09.json')));
const groups = {left_arm:[],right_arm:[],body:[],head:[]};
Object.keys(profile.joint_limits_deg).forEach(n => groups[n.startsWith('arm_l')?'left_arm':n.startsWith('arm_r')?'right_arm':n.startsWith('body')?'body':'head'].push(n));
const target = Object.assign({}, ...Object.keys(profile.joint_limits_deg).map(n => ({[n]:0})), {body_j3:-5,body_j4:-10,body_j5:-5});
const robot = {id:'F1-test',label:'模拟机器人',groups:groups,joint_limits_deg:profile.joint_limits_deg,capabilities:{joints:{available:true},gripper:{available:true},base:{available:true}}};
let authenticated=true, lease=null, current=null, posts=[], mode='ready', serial=0;
const eid='eeeeeeeeeeeeeeeeeeeeeeee';
const episode={id:eid,name:'f1_hil_20261013T140457Z_2bb86113',path:'/data/hil_data/demonstration/f1_hil_fixture',source:'hil_demonstration',execution:'powered',recording_status:'committed',source_signature:'fixture',size_bytes:479200000,started_at:'2026-10-13T14:04:57Z',files:Array.from({length:80},(_,i)=>({relative_path:'journal/record-'+i+'.jsonl',size_bytes:20000,modified_at:'2026-10-13T14:05:36Z'})),topics:[],videos:[],duration_s:39.1,review:{status:'pending'},replay_url:'/api/data/hil/'+eid+'/timeline'};
let catalog=Array.from({length:137},(_,i)=>Object.assign({},episode,{id:i===0?eid:i.toString(16).padStart(24,'0'),name:i===0?episode.name:'f1_hil_20261013_take_'+String(i).padStart(3,'0'),path:'/data/hil_data/demonstration/'+i,review:{status:i%2?'approved':'pending'}}));
function fixtureImage(camera){const wide=camera==='head',w=wide?1280:640,h=wide?720:480;return `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><rect width="${w}" height="${h}" fill="#e8edf4"/><path d="M0 ${h*.67} L${w*.5} ${h*.44} L${w} ${h*.67} V${h} H0" fill="#cfd9e5"/><circle cx="${w*.5}" cy="${h*.46}" r="${h*.12}" fill="#9bb0c5"/><text x="${w*.5}" y="${h*.85}" text-anchor="middle" fill="#566c84" font-family="sans-serif" font-size="24">${camera} · OFFLINE FIXTURE</text></svg>`;}
const plan = {motion_s:8.03,estimated_total_s:8.18,parts:[{label:'左臂',status:'moving'},{label:'右臂',status:'moving'},{label:'身体',status:'moving'},{label:'头部',status:'moving'}],checkpoint_count:2};
function state(){return {connected:true,authenticated:authenticated,control:{lease_id:lease,owner:lease?'browser':null},joints:Object.assign({},...Object.keys(target).map(n=>({[n]:{position:target[n],age_s:.01}}))),grippers:{left:{position:20,age_s:.01},right:{position:20,age_s:.01}},cameras:{},services:{},capabilities:robot.capabilities,action:current,system_health:{disk:{total_bytes:1e12,used_bytes:2.5e11,free_bytes:7.5e11}}};}
const server = http.createServer(async (req,res)=>{
 const url = new URL(req.url,'http://127.0.0.1');let raw='';for await(const chunk of req)raw+=chunk;
 const body=raw?JSON.parse(raw):null;if(req.method==='POST')posts.push({path:url.pathname,body:body});
 let data;
 if(url.pathname==='/api/robot')data={robot:robot};
 else if(url.pathname==='/api/session')data={authenticated:authenticated};
 else if(url.pathname==='/api/state')data=state();
 else if(url.pathname==='/api/data')data={episodes:catalog,available:true,summary:{episode_count:catalog.length},recording:{state:'no_process_observed'}};
 else if(url.pathname.startsWith('/api/data/episodes/'))data={episode:catalog.find(e=>e.id===url.pathname.split('/').pop())||episode};
 else if(url.pathname==='/api/data/reviews'){const row=catalog.find(e=>e.id===body.episode_id);row.review={status:body.status,notes:body.notes,updated_at:'2026-10-14T09:00:00Z'};data={review:row.review};}
 else if(url.pathname==='/api/data/delete'){const ids=body.episodes.map(e=>e.id);const deleted=catalog.filter(e=>ids.includes(e.id));catalog=catalog.filter(e=>!ids.includes(e.id));data={complete:true,transaction:'fake-delete',results:deleted.map(e=>({id:e.id,name:e.name,state:'deleted'}))};}
 else if(url.pathname.endsWith('/timeline'))data={duration_s:39.1,frames:1173,observed_hz:30};
 else if(url.pathname.endsWith('.jpg')){res.setHeader('Content-Type','image/svg+xml');res.setHeader('X-F1-Observation-Id','frame-1');res.setHeader('X-F1-Recording-Time',url.searchParams.get('t')||'0');res.end(fixtureImage(url.pathname.split('/').pop().replace('.jpg','')));return;}

 else if(url.pathname==='/api/replay/review')data={review:{executable:false,issues:['离线假数据'],can_align:false,events:[]}};
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
  await call('Runtime.enable');await call('Page.enable');await call('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false});
  await call('Page.navigate',{url:'http://127.0.0.1:'+port+'/#data'});
  await until("document.querySelectorAll('.recording-open').length===12",'bounded library');
  assert.strictEqual(await evaluate("document.querySelector('.data-pagination').textContent.includes('137')"),true);
  assert.strictEqual(await evaluate("document.documentElement.scrollWidth<=innerWidth"),true);
  assert.strictEqual(await evaluate("document.querySelector('.data-pagination').getBoundingClientRect().bottom<=innerHeight"),true);
  await snap('library-desktop');
  await click('[data-folder="DAgger"]');await click('[data-folder="DAgger/人工示教"]');
  await click('[data-click="data-select-start"]');await click('[data-click="data-select-all"]');
  assert.strictEqual(await evaluate("document.querySelectorAll('[aria-checked=true]').length"),12);
  await click('[data-data-page="2"]');
  assert.strictEqual(await evaluate("document.querySelector('.data-selection-count').textContent.includes('12')"),true);
  await click('[data-click="data-select-all"]');
  assert.strictEqual(await evaluate("document.querySelector('.data-selection-count').textContent.includes('24')"),true);
  await evaluate("window.confirm=()=>false");await click('[data-click="data-delete"]');
  assert.strictEqual(posts.length,0);
  await evaluate("window.confirm=()=>true");await click('[data-click="data-delete"]');
  await until("document.querySelector('.data-pagination').textContent.includes('113')",'delete refresh');
  const deletion=posts.find(p=>p.path==='/api/data/delete');assert.strictEqual(deletion.body.episodes.length,24);
  assert.strictEqual(deletion.body.episodes.every(e=>e.source_signature==='fixture'),true);
  await evaluate("const input=document.querySelector('#data-search');input.focus();input.value='take_13';input.dispatchEvent(new Event('input',{bubbles:true}))");
  assert.strictEqual(await evaluate("document.activeElement.id"),'data-search');
  assert.strictEqual(await evaluate("document.querySelectorAll('.recording-open').length"),7);
  await evaluate("document.querySelector('#data-review-filter').value='pending';document.querySelector('#data-review-filter').dispatchEvent(new Event('change',{bubbles:true}))");
  assert.strictEqual(await evaluate("document.querySelectorAll('.recording-open').length"),4);
  await click('[data-episode]');await until("document.querySelector('.episode-files')",'detail');
  assert.strictEqual(await evaluate("document.querySelector('#data-library-view').hidden"),true);
  assert.strictEqual(await evaluate("document.querySelector('.episode-files').open"),false);
  assert.strictEqual(await evaluate("document.querySelector('.episode-path').open"),false);
  await until("[...document.querySelectorAll('.hil-replay-grid img')].every(i=>i.naturalWidth>0)",'original images');
  assert.strictEqual(await evaluate("document.querySelector('[data-replay-play]').getBoundingClientRect().bottom<innerHeight"),true);
  await evaluate("document.querySelectorAll('.toast').forEach(t=>t.remove())");
  await snap('detail-desktop');
  await evaluate("document.querySelector('.episode-files').open=true");
  assert.strictEqual(await evaluate("document.querySelector('.episode-files-scroll').clientHeight<=300"),true);
  assert.strictEqual(await evaluate("document.querySelector('.episode-files-scroll').scrollHeight>300"),true);
  await evaluate("document.querySelector('.episode-files').open=false;document.querySelector('#review-notes').value='检查完成';document.querySelector('#review-notes').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#review-status').value='approved';document.querySelector('#review-form').requestSubmit()");
  await until("document.querySelector('#review-saved').textContent.includes('2026')",'save review');
  assert.strictEqual(posts.find(p=>p.path==='/api/data/reviews').body.expected_source_signature,'fixture');
  assert.strictEqual(await evaluate("document.querySelector('[data-episode-review]').textContent"),'已通过');
  await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await wait(250);
  assert.strictEqual(await evaluate("document.documentElement.scrollWidth<=innerWidth"),true);
  await evaluate("document.querySelectorAll('.toast').forEach(t=>t.remove());window.scrollTo(0,0)");
  assert.strictEqual(await evaluate("document.querySelector('[data-review-camera=head]').getBoundingClientRect().width>document.querySelector('[data-review-camera=left_wrist]').getBoundingClientRect().width*1.8"),true);
  assert.strictEqual(await evaluate("document.querySelector('[data-replay-play]').getBoundingClientRect().bottom<innerHeight"),true);
  await snap('detail-mobile');
  await click('[data-click="data-back"]');
  assert.strictEqual(await evaluate("document.querySelector('#data-search').value"),'take_13');
  assert.strictEqual(await evaluate("document.querySelector('#data-review-filter').value"),'pending');
  assert.strictEqual(await evaluate("document.documentElement.scrollWidth<=innerWidth"),true);
  await snap('library-mobile');
  assert.deepStrictEqual(exceptions,[]);
  assert.strictEqual(posts.some(p=>/replay|actions|control/.test(p.path)),false);
  const report={result:'passed',source:'isolated fake API; no robot connection',checks:['137 recordings bounded to 12 rows','source navigation','page-scoped select all','selection survives pagination','cancel deletion sends nothing','confirmed fake deletion uses signatures','search focus survives update','review filter','focused detail and return','files and paths collapsed','files scroll bounded to 300px','original images loaded','playback control visible above fold','review saved with source signature','desktop and mobile no overflow','no control or motion requests','no browser exceptions']};
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
 }finally{if(ws)ws.close();chrome.kill('SIGTERM');server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});

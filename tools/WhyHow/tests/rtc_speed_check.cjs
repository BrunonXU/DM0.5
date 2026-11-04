/* Browser acceptance for the read-only RTC speed lesson; no robot API calls. */
const http=require('http'),fs=require('fs'),path=require('path'),os=require('os'),cp=require('child_process'),WS=require('ws'),assert=require('assert');
const base=process.env.WHYHOW_TEST_URL||'http://127.0.0.1:18443';
const out=path.resolve(process.env.WHYHOW_SPEED_OUTPUT||path.join(__dirname,'../artifacts/rtc-speed-preview'));
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const get=url=>new Promise((resolve,reject)=>http.get(url,res=>{let b='';res.on('data',c=>b+=c);res.on('end',()=>{try{assert.equal(res.statusCode,200);resolve(JSON.parse(b))}catch(e){reject(e)}})}).on('error',reject));
(async()=>{
 fs.mkdirSync(out,{recursive:true});const report={url:base,checks:[],errors:[],started:new Date().toISOString()};
 const profile=fs.mkdtempSync(path.join(os.tmpdir(),'rtc-speed-browser-'));
 const chrome=cp.spawn('/usr/bin/google-chrome',['--headless=new','--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--no-proxy-server','--disable-background-networking','--disable-component-update','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{stdio:'ignore'});
 let ws;
 try{
  const active=path.join(profile,'DevToolsActivePort');for(let i=0;i<100&&!fs.existsSync(active);i++)await sleep(100);
  const port=fs.readFileSync(active,'utf8').split('\n')[0];const targets=await get('http://127.0.0.1:'+port+'/json/list');
  ws=new WS(targets.find(t=>t.type==='page').webSocketDebuggerUrl);await new Promise((r,j)=>{ws.once('open',r);ws.once('error',j)});
  let seq=0;const pending=new Map(),contexts=new Map();
  ws.on('message',raw=>{const m=JSON.parse(raw);if(pending.has(m.id)){const p=pending.get(m.id);clearTimeout(p.timer);pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result)}
   else if(m.method==='Runtime.exceptionThrown')report.errors.push(m.params.exceptionDetails);
   else if(m.method==='Runtime.executionContextCreated')contexts.set(m.params.context.id,m.params.context);
   else if(m.method==='Runtime.executionContextDestroyed')contexts.delete(m.params.executionContextId);
   else if(m.method==='Runtime.executionContextsCleared')contexts.clear();});
  const call=(method,params={},sessionId)=>new Promise((resolve,reject)=>{const id=++seq,timer=setTimeout(()=>{pending.delete(id);reject(Error('Timeout '+method))},30000);pending.set(id,{resolve,reject,timer});const message={id,method,params};if(sessionId)message.sessionId=sessionId;ws.send(JSON.stringify(message))});
  const evaluate=async(expression,contextId)=>{const p={expression,returnByValue:true,awaitPromise:true,userGesture:true};if(contextId)p.contextId=contextId;const r=await call('Runtime.evaluate',p);if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value};
  const until=async(expression,contextId)=>{for(let i=0;i<100;i++){if(await evaluate(expression,contextId))return;await sleep(100)}throw Error('Not ready: '+expression)};
  const shot=async name=>{const r=await call('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(out,name+'.png'),Buffer.from(r.data,'base64'))};
  const record=(name,value)=>report.checks.push({name,value});
  await call('Page.enable');await call('Runtime.enable');
  await call('Page.navigate',{url:base+'/interactive/rtc-speed/'});await until('Boolean(window.speedLesson)');
  await call('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  const initial=await evaluate('speedLesson.snapshot()');assert.equal(initial.r,1.2);assert.equal(initial.duration,5);assert(Math.abs(initial.task.fast-36.36363636)<.0001);
  record('default 1.2x: 6s→5s; 60s sync→43.636s RTC→36.364s',initial);
  const rows=await evaluate("Array.from(document.querySelectorAll('#rtc-table tr')).map(x=>x.textContent)");
  assert(rows[2].includes('17 / 20'));assert(rows[2].includes('472.2ms'));assert(rows[2].includes('555.6ms'));record('RTC budget uses ceil in wall-clock domain',rows);
  await evaluate("document.getElementById('wall').value=2;document.getElementById('wall').dispatchEvent(new Event('input'))");
  assert.equal((await evaluate('speedLesson.snapshot()')).phase,2.4);
  await evaluate("document.getElementById('play').click()");await sleep(360);
  assert((await evaluate('speedLesson.snapshot()')).t>2.1);
  await evaluate("document.getElementById('play').click()");const paused=(await evaluate('speedLesson.snapshot()')).t;await sleep(150);
  assert.equal((await evaluate('speedLesson.snapshot()')).t,paused);record('scrub, play and pause advance/freeze wall clock',{paused});
  await evaluate("document.getElementById('mode').value='travel';document.getElementById('mode').dispatchEvent(new Event('change'))");
  const travel=await evaluate('speedLesson.snapshot()');assert.equal(travel.t,0);assert(Math.abs(travel.duration-5.3)<1e-10);assert(Math.abs(travel.task.fast-38.545454545)<.0001);
  await evaluate("document.getElementById('wall').value=4;document.getElementById('wall').dispatchEvent(new Event('input'))");
  assert(Math.abs((await evaluate('speedLesson.snapshot()')).phase-4.7)<1e-10);
  record('70% travel-only speed: 5.3s, contact advances at 1x',travel);
  await evaluate("document.querySelector('[data-speed=\"1\"]').click()");assert.equal((await evaluate('speedLesson.snapshot()')).duration,6);
  await evaluate("document.querySelector('[data-speed=\"1.5\"]').click()");assert.equal((await evaluate('speedLesson.snapshot()')).r,1.5);
  await evaluate("document.querySelector('[data-speed=\"1.2\"]').click();document.querySelector('[data-latency=\"367\"]').click()");
  const margins=await evaluate("Array.from(document.querySelectorAll('#rtc-table td:last-child')).map(x=>({text:x.textContent,cls:x.className}))");
  assert.equal(margins[1].cls,'warn');assert(margins[1].text.includes('21.9'));record('historical 367ms: naive 1.2x leaves only 21.9ms',margins);
  await evaluate("document.querySelector('[data-latency=\"450\"]').click()");assert.equal(await evaluate("document.querySelectorAll('#rtc-table td:last-child')[1].className"),'bad');
  record('deadline miss marked red',true);
  await evaluate("document.getElementById('latency').value=550;document.getElementById('latency').dispatchEvent(new Event('input'))");
  assert((await evaluate("document.getElementById('task-condition').textContent")).includes('条件不成立'));
  record('task duration formula explicitly invalidated when RTC cannot cover latency',true);
  await evaluate("document.querySelector('[data-latency=\"367\"]').click();document.getElementById('mode').value='all';document.getElementById('mode').dispatchEvent(new Event('change'));document.getElementById('wall').value=3;document.getElementById('wall').dispatchEvent(new Event('input'))");
  for(const width of [1280,1440,1920]){await call('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:false});assert.equal(await evaluate('document.documentElement.scrollWidth>innerWidth'),false);await evaluate("document.getElementById('motion').scrollIntoView({behavior:'instant'})");await shot('motion-'+width)}
  record('three desktop widths without horizontal overflow',[1280,1440,1920]);
  await call('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});await evaluate("document.getElementById('rtc').scrollIntoView({behavior:'instant'})");await shot('rtc-budget');
  await evaluate("document.getElementById('duration').scrollIntoView({behavior:'instant'})");await shot('task-duration');
  await call('Page.navigate',{url:base+'/?html=rtc-speed'});
  await until("document.getElementById('interactive')?.hidden===false && document.getElementById('iframe-status')?.hidden===true");
  assert.equal(await evaluate("document.getElementById('open-interactive').getAttribute('href')"),'/interactive/rtc-speed/');
  const tree=await call('Page.getFrameTree');const frame=(tree.frameTree.childFrames||[]).find(x=>x.frame.url.endsWith('/interactive/rtc-speed/'));let frameEval;
  if(frame){let context;for(let i=0;i<50;i++){context=[...contexts.values()].find(c=>c.auxData&&c.auxData.frameId===frame.frame.id&&c.auxData.isDefault);if(context)break;await sleep(100)}assert(context,'Sandboxed iframe context');frameEval=expr=>evaluate(expr,context.id)}
  else{const frames=(await call('Target.getTargets')).targetInfos;const target=frames.find(t=>t.type==='iframe'&&t.url.endsWith('/interactive/rtc-speed/'));assert(target,'Sandboxed iframe target');const attached=await call('Target.attachToTarget',{targetId:target.targetId,flatten:true});await call('Runtime.enable',{},attached.sessionId);frameEval=async expression=>{const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true,userGesture:true},attached.sessionId);if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value}}
  assert(await frameEval('Boolean(window.speedLesson)'));
  await frameEval("document.querySelector('[data-speed=\"1.1\"]').click()");assert.equal((await frameEval('speedLesson.snapshot()')).r,1.1);
  assert.equal(await frameEval('document.documentElement.scrollWidth>innerWidth'),false);
  await shot('reader-iframe');record('Why&How catalog route and sandboxed iframe controls',true);
  assert.equal(report.errors.length,0,'No browser exceptions');report.passed=true;
 }catch(e){report.passed=false;report.failure=String(e);process.exitCode=1}
 finally{report.finished=new Date().toISOString();fs.writeFileSync(path.join(out,'report.json'),JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report,null,2));if(ws)ws.close();chrome.kill('SIGTERM');await sleep(200);if(chrome.exitCode===null)chrome.kill('SIGKILL')}
})().catch(e=>{console.error(e);process.exitCode=1});

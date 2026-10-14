/* Offline/read-only HIL browser acceptance. Never calls action endpoints. */
const fs=require('fs'),http=require('http'),assert=require('assert'),WebSocket=require('ws');
const port=process.env.HIL_DEBUG_PORT||19443,base=process.env.HIL_REVIEW_URL||'http://127.0.0.1:18443/';
const output=process.env.HIL_REVIEW_OUTPUT||'results/hil_workspace/browser';fs.mkdirSync(output,{recursive:true});
assert(['127.0.0.1','localhost','[::1]'].includes(new URL(base).hostname),'Use a local offline preview');
const get=url=>new Promise((resolve,reject)=>http.get(url,r=>{let s='';r.on('data',x=>s+=x);r.on('end',()=>resolve(JSON.parse(s)));}).on('error',reject));
const wait=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{const tabs=await get(`http://127.0.0.1:${port}/json/list`),ws=new WebSocket(tabs.find(t=>t.type==='page').webSocketDebuggerUrl);await new Promise(r=>ws.on('open',r));let id=0;const pending=new Map(),errors=[],requests=[];
ws.on('message',raw=>{const x=JSON.parse(raw);if(x.id&&pending.has(x.id)){const p=pending.get(x.id);pending.delete(x.id);x.error?p.reject(Error(JSON.stringify(x.error))):p.resolve(x.result);}else if(x.method==='Runtime.exceptionThrown')errors.push(x.params.exceptionDetails);else if(x.method==='Network.requestWillBeSent')requests.push(x.params.request);});
const call=(method,params={})=>new Promise((resolve,reject)=>{const n=++id;pending.set(n,{resolve,reject});ws.send(JSON.stringify({id:n,method,params}));});
const evaluate=async expression=>{const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
await call('Runtime.enable');await call('Network.enable');await call('Network.setBlockedURLs',{urls:['*/api/dev/revision']});await call('Page.enable');await call('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
const routesFixture=`(()=>{const original=window.fetch;const routes=[['play','自由接入','teleop_play'],['demonstration','人工示教','demonstration'],['policy','策略 HIL','policy_hil']].map(([id,title,purpose])=>({id,title,purpose,ready:id!=='policy',reason:id==='policy'?'模型未绑定':'',commands:['F1 运行端','Windows 隧道','Windows 手柄端'].map((where,i)=>({id:'step'+i,where,command:'echo offline-fixture',hint:'离线测试命令'}))}));window.fetch=(url,options={})=>{if(options.method&&options.method!=='GET')throw Error('Mutating requests forbidden during HIL review');return new URL(url,location.href).pathname==='/api/hil/routes'?Promise.resolve(new Response(JSON.stringify({routes}),{headers:{'Content-Type':'application/json'}})):original(url,options);};})()`;
await call('Page.navigate',{url:base+'#hil'});await wait(1600);await evaluate(routesFixture);await wait(2200);
const report={initial:await evaluate(`({h1:document.querySelector('h1')?.innerText,text:document.querySelector('#main').innerText,details:[...document.querySelectorAll('#main details')].map(x=>x.open),overflow:document.documentElement.scrollWidth>innerWidth})`)};
assert.equal(await evaluate(`document.querySelectorAll('.hil-route-card').length`),3);assert(!report.initial.overflow);assert(report.initial.details.every(x=>!x));
await evaluate(`location.hash='hil?route=demonstration'`);await wait(1000);
report.workspace=await evaluate(`({closed:[...document.querySelectorAll('.hil-tools details')].every(x=>!x.open),overviewHeight:document.querySelector('#hil-overview').getBoundingClientRect().height,cameraTop:document.querySelector('[data-camera-preview]').getBoundingClientRect().top})`);assert(report.workspace.closed);assert(report.workspace.overviewHeight<170);assert(report.workspace.cameraTop<500);
await evaluate(`document.querySelector('#hil-start').open=true;document.querySelector('.hil-step').open=true`);await wait(1300);
assert(await evaluate(`document.querySelector('#hil-start').open&&document.querySelector('.hil-step').open`));assert.equal(await evaluate(`document.querySelectorAll('[data-hil-copy]:not([data-hil-copy=diagnostic])').length`),3);
await evaluate(`document.querySelector('#hil-start').open=false`);
let shot=await call('Page.captureScreenshot',{format:'png'});fs.writeFileSync(output+'/desktop.png',Buffer.from(shot.data,'base64'));
// Inject only synthetic GET responses to exercise live/expired/failure rendering.
await evaluate(`(()=>{const original=window.fetch;window.__hilFixture=null;window.fetch=(url,...args)=>String(url).endsWith('/api/hil/state')&&window.__hilFixture?Promise.resolve(new Response(JSON.stringify({hil:window.__hilFixture}),{headers:{'Content-Type':'application/json'}})):original(url,...args);})()`);
report.states={};
for(const [phase,label] of [['hil_latched','人工接管'],['safe_hold','保护保持'],['unknown','状态未知']]){
 const d={active:true,summary_fresh:phase==='hil_latched',phase,next_step:'测试提示：按住 RB 控臂。',commands:[],events:[],summary_age_s:1,status:phase==='hil_latched'?{execution:'POWERED',input_fresh:true,side:'right',rb:true,observation_age_ms:30,recording:{completed:100}}:{}};
 await evaluate(`window.__hilFixture=${JSON.stringify(d)}`);await wait(1200);const txt=await evaluate(`document.querySelector('#hil-overview').innerText`);assert(txt.includes(label));if(phase==='unknown')assert(!txt.includes('已收到'));report.states[phase]=txt;
}
const live={active:true,summary_fresh:true,summary_age_s:0,source:'structured_snapshot',phase:'hil_latched',can_finish:true,run_id:'fixture',session_key:'fixture',events:[],status:{purpose:'demonstration',execution:'POWERED',input_fresh:true,side:'right',rb:true,camera_age_ms:{left:20,head:20,right:20},recording:{phase:'recording',completed:100}}};
await evaluate(`window.__hilFixture=${JSON.stringify(live)}`);await wait(350);
assert(await evaluate(`!document.querySelector('[data-hil-finish]').disabled&&!document.querySelector('.hil-session-help').open`));
await evaluate(`document.querySelector('.hil-session-help').open=true`);await wait(300);
assert(await evaluate(`document.querySelector('#hil-recording-session').innerText.includes('Y 复位会被取消')`));
await evaluate(`document.querySelector('.hil-session-help').open=false`);
report.home=[];
for(const [stage,label] of [['align','起点对齐'],['lift','上抬'],['right','向右避让'],['return','回位']]){
 await evaluate(`window.__hilFixture.status.home_stage=${JSON.stringify(stage)}`);await wait(300);
 const text=await evaluate(`document.querySelector('#hil-overview h2').innerText`);assert(text.includes(label));report.home.push(text);
}
await evaluate(`window.__hilFixture.status.home_stage=null;window.__hilFixture.status.home_stop_reason='clearance Home lift tracking error: 5.24 deg';window.__hilFixture.status.side=null`);await wait(350);
assert(await evaluate(`document.querySelector('#hil-overview').innerText.includes('十字键选右臂')`));
await evaluate(`document.querySelector('[data-home-reason]').open=true`);await wait(500);
assert(await evaluate(`document.querySelector('[data-home-reason]').open&&document.querySelector('[data-home-reason]').innerText.includes('tracking error')`),'Fault details must survive polling');
await evaluate(`window.__hilFixture.status.side='right'`);await wait(300);assert(await evaluate(`document.querySelector('#hil-overview').innerText.includes('可按住 RB')`));
await evaluate(`window.__hilFixture.status.home_stop_reason=null;window.__hilFixture.status.execution=null`);await wait(300);
assert.equal(await evaluate(`document.querySelector('.hil-mode').innerText`),'未确认执行权限');
await evaluate(`window.__hilFixture.summary_age_s=3`);await wait(300);
report.expired=await evaluate(`({text:document.querySelector('#hil-overview').innerText,canFinish:!document.querySelector('[data-hil-finish]').disabled})`);
assert(report.expired.text.includes('状态未知'));assert(!report.expired.text.includes('新鲜'));assert(!report.expired.canFinish);
await evaluate(`window.__hilFixture=${JSON.stringify(live)};window.__hilFixture.phase='safe_hold';window.__hilFixture.status.hold_reason='feedback_stale'`);await wait(300);
assert(await evaluate(`document.querySelector('#hil-overview').innerText.includes('机器人反馈超时')`));
await evaluate(`window.__hilFixture=${JSON.stringify(live)};window.__hilFixture.status.purpose='policy_hil'`);await wait(300);
assert(await evaluate(`document.querySelector('[data-hil-finish]').disabled`),'Other route cannot finish current session');
await evaluate(`location.hash='hil?route=policy'`);await wait(500);
assert(await evaluate(`document.querySelector('[data-hil-copy=step0]').disabled`),'Unconfigured model cannot copy run command');
await evaluate(`location.hash='hil?route=demonstration';window.__hilFixture=${JSON.stringify(live)}`);await wait(500);
await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await wait(300);report.mobile=await evaluate(`({width:innerWidth,scroll:document.documentElement.scrollWidth})`);assert(report.mobile.scroll<=report.mobile.width);
shot=await call('Page.captureScreenshot',{format:'png'});fs.writeFileSync(output+'/mobile.png',Buffer.from(shot.data,'base64'));
await evaluate(`location.hash='hil'`);await wait(350);const requestCursor=requests.length;await wait(600);assert(!requests.slice(requestCursor).some(r=>r.url.includes('/api/cameras/')));
report.exceptions=errors;report.non_get=requests.filter(r=>r.method!=='GET').map(r=>r.url);report.camera_requests=requests.filter(r=>r.url.includes('/api/cameras/')).length;
assert.equal(errors.length,0);assert.equal(report.non_get.length,0);assert(report.camera_requests>0);
fs.writeFileSync(output+'/validation.json',JSON.stringify(report,null,2));console.log(JSON.stringify({ok:true,mobile:report.mobile,errors:errors.length,non_get:report.non_get.length,camera_requests:report.camera_requests}));ws.close();
})().catch(e=>{console.error(e);process.exit(1)});

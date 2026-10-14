const fs=require('fs'),os=require('os'),path=require('path'),http=require('http');
const {spawn}=require('child_process'); const assert=require('assert');
const root=path.resolve(__dirname,'..');
const temp=fs.mkdtempSync(path.join(os.tmpdir(),'whereweare-editorial-live-'));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let server, chrome;
let sequence = 0;
let buffer = '';
const pending = new Map();
const errors = [];
let session;
let bridgeBase;
function send(method, params, sessionId) {
  return new Promise((resolve, reject) => {
    const id = ++sequence;
    const timeout = setTimeout(() => { pending.delete(id); reject(new Error('CDP timeout: ' + method)); }, 15000);
    pending.set(id, {resolve, reject, timeout});
    const command = {id, method, params: params || {}};
    if (sessionId) command.sessionId = sessionId;
    chrome.stdio[3].write(JSON.stringify(command) + '\0');
  });
}
function onData(chunk) {
  buffer += chunk.toString();
  let end;
  while ((end = buffer.indexOf('\0')) >= 0) {
    const raw = buffer.slice(0, end); buffer = buffer.slice(end + 1);
    if (!raw) continue;
    const message = JSON.parse(raw);
    if (message.id && pending.has(message.id)) {
      const request = pending.get(message.id); pending.delete(message.id); clearTimeout(request.timeout);
      if (message.error) request.reject(new Error(JSON.stringify(message.error))); else request.resolve(message.result);
    } else if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails);
    else if (message.method === 'Runtime.bindingCalled' && message.params.name === 'hubTestFetch') bridgeFetch(message.params.payload);
  }
}
async function evaluate(expression) {
  const result = await send('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true}, session);
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
// This environment stalls Chrome's native network stack before a socket is opened.
// Render the real assets in Chrome and transport fetch through Node to the real
// isolated backend. This tests UI/backend behavior, not Chrome-native networking.
async function bridgeFetch(payload) {
  let request;
  try {
    request = JSON.parse(payload); if ((request.options.method || 'GET') !== 'GET') throw new Error('Read-only preview');
    const target = new URL(request.url, bridgeBase);
    if (target.origin !== bridgeBase) throw new Error('Blocked non-test-origin request');
    const response = await new Promise((resolve, reject) => {
      const headers = Object.assign({}, request.options.headers || {});
      if (!Object.keys(headers).some(name => name.toLowerCase() === 'origin')) headers.Origin = bridgeBase;
      if (request.options.body !== undefined) headers['Content-Length'] = Buffer.byteLength(request.options.body);
      const transport = http.request(target, {method: request.options.method || 'GET', headers}, res => {
        const chunks = [];
        res.on('data', chunk => chunks.push(chunk));
        res.on('end', () => resolve({status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString('utf8')}));
      });
      transport.on('error', reject);
      transport.setTimeout(10000, () => transport.destroy(new Error('Local test backend timeout')));
      if (request.options.body) transport.write(request.options.body);
      transport.end();
    });
    await evaluate('window.__hubTestFetchResolve(' + JSON.stringify(request.id) + ',' + JSON.stringify(response) + ',null)');
  } catch (error) {
    if (request) { try { await evaluate('window.__hubTestFetchResolve(' + JSON.stringify(request.id) + ',null,' + JSON.stringify(error.message) + ')'); } catch (_) {} }
  }
}
async function reloadApp() {
  await send('Page.reload', {}, session);
  await delay(150);
  const html = (await get(bridgeBase + '/')).data;
  const css = (await get(bridgeBase + '/styles.css')).data;
  const script = (await get(bridgeBase + '/app.js')).data;
  const tree = await send('Page.getFrameTree', {}, session);
  const content = html.replace(/<link\b[^>]*href="\/styles\.css"[^>]*>/, '<style>' + css + '</style>').replace(/<script\b[^>]*src="\/app\.js"[^>]*><\/script>/, '');
  await send('Page.setDocumentContent', {frameId: tree.frameTree.frame.id, html: content}, session);
  await send('Runtime.addBinding', {name: 'hubTestFetch'}, session);
  await evaluate('(function(){var next=0,pending=new Map();window.__hubTestFetchResolve=function(id,data,error){var p=pending.get(id);if(!p)return;pending.delete(id);if(error)p.reject(new Error(error));else p.resolve(new Response(data.body,{status:data.status,headers:data.headers}));};window.fetch=function(url,options){return new Promise(function(resolve,reject){var id=++next;pending.set(id,{resolve:resolve,reject:reject});window.hubTestFetch(JSON.stringify({id:id,url:String(url),options:options||{}}));});};})()');
  await evaluate(script);
}
function get(url) {
  return new Promise((resolve, reject) => {
    http.get(url, res => {
      let data = ''; res.on('data', b => data += b); res.on('end', () => resolve({status: res.statusCode, data}));
    }).on('error', reject);
  });
}
async function until(fn, label) {
  for (let i = 0; i < 180; i++) { if (await fn()) return; await delay(100); }
  throw new Error('Wait failed: ' + label);
}
async function click(selector) {
  return evaluate('(function(){var el=document.querySelector(' + JSON.stringify(selector) + ');if(!el)throw Error("Missing element");el.dispatchEvent(new MouseEvent("click",{bubbles:true}));return true;})()');
}
async function fill(values) {
  return evaluate('(function(){var values=' + JSON.stringify(values) + ';Object.keys(values).forEach(function(id){var el=document.getElementById(id);if(!el)throw Error("Missing input "+id);if(el.type==="checkbox")el.checked=values[id];else el.value=values[id];el.dispatchEvent(new Event("input",{bubbles:true}));el.dispatchEvent(new Event("change",{bubbles:true}));});return true;})()');
}


(async function(){
 bridgeBase='http://127.0.0.1:4343';
 const state=JSON.parse((await get(bridgeBase+'/api/state')).data);
 assert.strictEqual(state.projects.find(p=>p.id==='block-sort').tracking.activity,'current');
 assert(state.whereweare.snapshot.revision>=5);
 
 const log=fs.openSync(path.join(temp,'chrome.log'),'w');
 chrome=spawn('google-chrome',['--headless=new','--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--no-first-run','--no-proxy-server','--disable-background-networking','--remote-debugging-pipe','--user-data-dir='+path.join(temp,'chrome-profile'),'about:blank'],{stdio:['ignore',log,log,'pipe','pipe']});
 chrome.stdio[4].on('data',onData);
 const target=await send('Target.createTarget',{url:'about:blank'});
 session=(await send('Target.attachToTarget',{targetId:target.targetId,flatten:true})).sessionId;
 await send('Page.enable',{},session);await send('Runtime.enable',{},session);
 await send('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false},session);
 await reloadApp();
 await until(async()=>evaluate('document.getElementById("timeline").dataset.project === "block-sort"'),'Live F1 mainline');
 assert.strictEqual(await evaluate('document.querySelectorAll("#timeline [data-project-line]").length'),3);
 const title=await evaluate('document.getElementById("project-title").textContent');
 assert(title.includes('F1')&&title.includes('积木'));
 async function shot(name){await evaluate('window.scrollTo(0,0)');const s=await send('Page.captureScreenshot',{format:'png'},session);fs.writeFileSync(path.join(temp,name+'.png'),Buffer.from(s.data,'base64'));}
 async function metrics(){return evaluate(`(()=>{const stage=document.getElementById('graphstage'),svg=document.getElementById('timeline'),view=document.getElementById('plot-viewport');const nodes=[...document.querySelectorAll('.node-title')].map(n=>{let r=n.getBoundingClientRect();return {text:n.textContent,x:r.x,y:r.y,w:r.width,h:r.height,font:getComputedStyle(n).fontSize};});return {width:innerWidth,overflow:document.documentElement.scrollWidth>innerWidth,bottom:stage.getBoundingClientRect().bottom,nowRatio:(view.getBoundingClientRect().left+Number(svg.dataset.currentX))/innerWidth,nodes};})()`);}
 const core=await metrics();await shot('desktop');assert(core.bottom<=900);assert(!core.overflow);assert(core.nowRatio>.5&&core.nowRatio<.8);
 function overlaps(m){const ns=m.nodes;for(let i=0;i<ns.length;i++)for(let j=i+1;j<ns.length;j++){const a=ns[i],b=ns[j];assert(!(a.x<b.x+b.w&&a.x+a.w>b.x&&a.y<b.y+b.h&&a.y+a.h>b.y),'Overlapping labels: '+a.text+' / '+b.text);}}
 overlaps(core);
 await click('#project-heading');await shot('detail');await click('#back-global');
 await click('[data-action="library"]');await shot('library');
 assert(await evaluate('document.querySelector(".repository-link").href==="https://github.com/BrunonXU/DM0.5"'));
 await click('#detail-dialog [data-action="close"]');
 await send('Emulation.setDeviceMetricsOverride',{width:1920,height:1080,deviceScaleFactor:1,mobile:false},session);await delay(180);await click('[data-action="range"][data-range="fortnight"]');await shot('wide');
 const wide=await metrics();assert(wide.bottom<=1080);assert(!wide.overflow);overlaps(wide);
 assert.deepStrictEqual(errors,[]);
 const result={ok:true,artifacts:temp,core,wide,revision:state.whereweare.snapshot.revision,javascriptErrors:errors.length};fs.writeFileSync(path.join(temp,'result.json'),JSON.stringify(result,null,2));console.log(JSON.stringify({ok:true,artifacts:temp,desktopBottom:core.bottom,wideBottom:wide.bottom,nowRatio:core.nowRatio,javascriptErrors:errors.length}));
})().catch(e=>{console.error(e.stack);console.error('Artifacts: '+temp);process.exitCode=1;}).finally(async()=>{if(chrome){try{await send('Browser.close');}catch(_){chrome.kill('SIGTERM');}}});

const http=require('http'),fs=require('fs'),path=require('path'),os=require('os'),cp=require('child_process'),WS=require('ws');
const root=path.resolve(__dirname,'../artifacts/browser-redesign-production'), pause=ms=>new Promise(r=>setTimeout(r,ms));
function get(url){return new Promise((resolve,reject)=>{http.get(url,r=>{let b='';r.on('data',c=>b+=c);r.on('end',()=>{try{resolve(JSON.parse(b))}catch(e){reject(e)}})}).on('error',reject)})}
(async()=>{
 fs.mkdirSync(root,{recursive:true});
 const base=process.env.WHYHOW_TEST_URL || 'http://127.0.0.1:4243';
 const profile=fs.mkdtempSync(path.join(os.tmpdir(),'whyhow-reading-preview-'));
 const chrome=cp.spawn('/usr/bin/google-chrome',['--headless=new','--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--no-proxy-server','--disable-background-networking','--disable-component-update','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{stdio:'ignore'});
 let ws;
 try{
  const active=path.join(profile,'DevToolsActivePort');for(let i=0;i<100&&!fs.existsSync(active);i++)await pause(100);
  const port=fs.readFileSync(active,'utf8').split('\n')[0],targets=await get('http://127.0.0.1:'+port+'/json/list');
  ws=new WS(targets.find(t=>t.type==='page').webSocketDebuggerUrl);await new Promise((r,j)=>{ws.once('open',r);ws.once('error',j)});
  let seq=0;const pending=new Map(),errors=[];
  ws.on('message',raw=>{const m=JSON.parse(raw);if(pending.has(m.id)){const p=pending.get(m.id);clearTimeout(p.timer);pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result)}else if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails)});
  const call=(method,params={})=>new Promise((resolve,reject)=>{const id=++seq,timer=setTimeout(()=>{pending.delete(id);reject(Error('Timeout '+method))},90000);pending.set(id,{resolve,reject,timer});ws.send(JSON.stringify({id,method,params}))});
  const evaluate=async expression=>{const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true,userGesture:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value};
  const screenshot=async name=>{const r=await call('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(root,name),Buffer.from(r.data,'base64'))};
  await call('Page.enable');await call('Runtime.enable');
  async function until(expression){for(let i=0;i<150;i++){if(await evaluate(expression))return;await pause(100)}throw Error('Timed out: '+expression)}
  await call('Page.navigate',{url:base+'/'});
  await until("document.getElementById('document')?.hidden===false && document.querySelectorAll('code.language-mermaid').length===0");
  await call('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false});
  const first=await evaluate("({kind:document.body.dataset.kind,font:getComputedStyle(document.getElementById('markdown-body')).fontSize,overflow:document.documentElement.scrollWidth>innerWidth})");
  if(first.kind!=='markdown'||first.font!=='22px'||first.overflow)throw Error('Default reader layout: '+JSON.stringify(first));
  await screenshot('reader-live.png');
  await evaluate("document.getElementById('open-library').click();document.getElementById('search').value='no-matching-article';document.getElementById('search').dispatchEvent(new Event('input',{bubbles:true}))");
  if(!await evaluate("document.querySelector('#catalog .sidebar-message').textContent.includes('没有找到')"))throw Error('Missing empty-search state');
  await screenshot('empty-search.png');
  await evaluate("document.getElementById('close-library').click()");
  await call('Page.navigate',{url:base+'/?doc=missing-redesign-check'});
  await until("document.getElementById('error')?.hidden===false");
  await screenshot('error-state.png');
  await evaluate("history.replaceState({},'', '/?doc=reset-and-tail');document.getElementById('retry').click()");
  await until("document.getElementById('document')?.hidden===false && document.querySelectorAll('code.language-mermaid').length===0");
  await screenshot('reset-article-live.png');
  await call('Page.navigate',{url:base+'/interactive/action-windows/'});
  await until("document.getElementById('startLabel')?.textContent.length>0");
  if(await evaluate('getComputedStyle(document.body).fontSize')!=='18px')throw Error('Standalone HTML theme missing');
  await evaluate("document.getElementById('start').value='140';document.getElementById('start').dispatchEvent(new Event('input',{bubbles:true}))");
  if(!await evaluate("document.getElementById('startLabel').textContent.includes('140')"))throw Error('Standalone slider');
  await screenshot('interactive-standalone-live.png');
  if(errors.length)throw Error('Page exceptions: '+JSON.stringify(errors));
  const report={result:'PASS',base,first,emptySearch:true,errorRetry:true,standaloneTheme:true,standaloneSlider:true,exceptions:errors,finished_at:new Date().toISOString()};
  fs.writeFileSync(path.join(root,'report.json'),JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify(report));
 }finally{if(ws)ws.close();chrome.kill('SIGTERM')}
})().catch(e=>{console.error(e);process.exitCode=1});

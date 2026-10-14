/* Read-only browser audit via Chrome DevTools. Never sends robot requests. */
const http=require('http'), fs=require('fs'), path=require('path'), WebSocket=require('ws');
const base=process.env.F1_REVIEW_URL||'http://127.0.0.1:18442/';
const output=process.env.F1_REVIEW_OUTPUT||path.resolve(__dirname,'../results/redesign_20260923/browser');
const debugPort=Number(process.env.F1_REVIEW_DEBUG_PORT||19442);
fs.mkdirSync(output,{recursive:true});
function get(url){return new Promise((resolve,reject)=>http.get(url,r=>{let b='';r.on('data',x=>b+=x);r.on('end',()=>{try{resolve(JSON.parse(b))}catch(e){reject(e)}})}).on('error',reject));}
const wait=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 const tabs=await get(`http://127.0.0.1:${debugPort}/json/list`), tab=tabs.find(t=>t.type==='page');
 const ws=new WebSocket(tab.webSocketDebuggerUrl);await new Promise((r,j)=>{ws.on('open',r);ws.on('error',j)});
 let id=0;const pending=new Map(),exceptions=[],requests=[];
 ws.on('message',raw=>{const p=JSON.parse(raw);if(p.id&&pending.has(p.id)){const h=pending.get(p.id);pending.delete(p.id);p.error?h.reject(Error(JSON.stringify(p.error))):h.resolve(p.result)}else if(p.method==='Runtime.exceptionThrown')exceptions.push(p.params.exceptionDetails);else if(p.method==='Network.requestWillBeSent')requests.push({method:p.params.request.method,url:p.params.request.url});});
 const call=(method,params={})=>new Promise((resolve,reject)=>{const n=++id;pending.set(n,{resolve,reject});ws.send(JSON.stringify({id:n,method,params}));});
 const evaluate=async expression=>{const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
 await call('Runtime.enable');await call('Network.enable');await call('Page.enable');
 await call('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
 const report={base,checked_at:new Date().toISOString(),pages:{}};
 for(const name of ['overview','control','pico','data','system']){
  await call('Page.navigate',{url:base+'#'+name});await wait(name==='control'?7500:1800);
  report.pages[name]=await evaluate(`({h1:document.querySelector('h1')?.innerText,overflow:document.documentElement.scrollWidth>innerWidth,body_font:getComputedStyle(document.body).fontSize,cameras:document.querySelectorAll('[data-camera]').length,sliders:document.querySelectorAll('input[type=range]').length,canvas:document.querySelectorAll('canvas').length,buttons:[...document.querySelectorAll('button')].filter(x=>x.offsetParent).map(x=>x.innerText).slice(0,35),text:document.body.innerText.slice(0,4500)})`);
  if(['overview','control','data'].includes(name)){const shot=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(output,name+'.png'),Buffer.from(shot.data,'base64'));}
 }
 await call('Page.navigate',{url:base+'#control'});await wait(2000);
 report.control=await evaluate(`({sliders:[...document.querySelectorAll('input[type=range]')].map(x=>({key:x.dataset.jointSlider||x.dataset.jointInput||x.dataset.joint||x.dataset.gripperSlider,value:x.value,min:x.min,max:x.max})),numbers:[...document.querySelectorAll('[data-joint-input]')].map(x=>({key:x.dataset.jointInput,value:x.value})),viewer:document.querySelector('#robot-viewer')?.innerText||null})`);
 const slider=await evaluate(`(()=>{const e=document.querySelector('input[type=range]:not(:disabled)');if(!e)return null;const before=e.value;e.value=String(Math.min(+e.max,+e.value+1));e.dispatchEvent(new Event('input',{bubbles:true}));return {before,after:e.value,key:e.dataset.jointSlider||e.dataset.jointInput||e.dataset.joint}})()`);await wait(500);report.slider_edit=slider;
 report.after_edit=await evaluate(`({numbers:[...document.querySelectorAll('[data-joint-input]')].map(x=>({key:x.dataset.jointInput,value:x.value})),text:document.body.innerText.slice(-1200)})`);
 await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await wait(1200);
 report.mobile=await evaluate(`({width:innerWidth,scroll:document.documentElement.scrollWidth})`);
 const shot=await call('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(output,'mobile.png'),Buffer.from(shot.data,'base64'));
 report.exceptions=exceptions;report.mutating_requests=requests.filter(r=>r.method!=='GET');
 fs.writeFileSync(path.join(output,'validation.json'),JSON.stringify(report,null,2));
 console.log(JSON.stringify({output,pages:Object.fromEntries(Object.entries(report.pages).map(([k,v])=>[k,{h1:v.h1,overflow:v.overflow,font:v.body_font,sliders:v.sliders,canvas:v.canvas}])),mobile:report.mobile,exceptions:exceptions.length,mutating_requests:report.mutating_requests},null,2));
 ws.close();
})().catch(e=>{console.error(e);process.exitCode=1});

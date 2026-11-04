/* Read-only report presentation, served locally; never connects to a robot. */
const fs=require('fs'),path=require('path'),http=require('http'),cp=require('child_process'),os=require('os'),assert=require('assert'),WS=require('ws');
const root=path.resolve(__dirname,'..'),web=path.resolve(process.env.F1_REPLAY_BROWSER_WEB_ROOT||path.join(root,'web')),out=path.resolve(process.env.F1_REPLAY_BROWSER_OUTPUT||path.join(root,'results/replay-results-ui/browser'));
const series=Array.from({length:80},(_,i)=>[i*.5,Math.sin(i/9)*20,Math.sin(i/9)*20+Math.sin(i/5)*.15]);
const channels=Array.from({length:16},(_,i)=>({name:i<14?`arm_${i<7?'l':'r'}_j${i%7+1}`:i===14?'left_gripper':'right_gripper',unit:i<14?'deg':'%',matched:79,replay_samples:80,metrics:{mean_abs:.01*(i+1),rms:.012*(i+1),max_abs:.03*(i+1),max_at_s:16.84},points:series}));
const summary=(count,mean)=>({complete:true,joints_compared:count,joints_total:count,metrics:{mean_abs:mean,rms:.057,max_abs:.559}});
const fake={result:{status:'succeeded',playback_timing:{total_shift_s:.04270766}},comparison:{available:true,channels,arms_summary:{both:summary(14,.037),left:summary(7,.047),right:summary(7,.027)}}};
const report=process.env.F1_REPLAY_REPORT_FIXTURE?JSON.parse(fs.readFileSync(process.env.F1_REPLAY_REPORT_FIXTURE)).report:fake;
let writes=0;
const server=http.createServer((req,res)=>{if(req.method!=='GET'){writes++;res.writeHead(405);res.end();return;}
 if(req.url==='/'){res.setHeader('Content-Type','text/html');res.end('<html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/styles.css"><link rel="stylesheet" href="/replay-report.css"><style>body{background:#f3f2ef;margin:0;padding:24px;display:block}main{max-width:1240px;margin:auto}.motion-replay{margin:0!important;padding:0!important;border:0!important;background:transparent!important}@media(max-width:700px){body{padding:10px}}</style><main class="motion-replay"><div id="report"></div></main><script type="module">import {renderReplayReport} from "/replay-report.js";window.renderReport=r=>renderReplayReport(document.querySelector("#report"),r);</script></html>');return;}
 const p=path.resolve(web,req.url.slice(1));if(!p.startsWith(web+path.sep)||!fs.existsSync(p)){res.writeHead(404);res.end();return;}res.setHeader('Content-Type',p.endsWith('.css')?'text/css':'text/javascript');fs.createReadStream(p).pipe(res);
});
const wait=ms=>new Promise(r=>setTimeout(r,ms)),get=url=>new Promise((ok,bad)=>http.get(url,r=>{let s='';r.on('data',d=>s+=d);r.on('end',()=>ok(JSON.parse(s)));}).on('error',bad));
(async()=>{fs.mkdirSync(out,{recursive:true});await new Promise(r=>server.listen(0,'127.0.0.1',r));const profile=fs.mkdtempSync(path.join(os.tmpdir(),'replay-report-'));
 const chrome=cp.spawn('/usr/bin/google-chrome',['--headless=new','--no-sandbox','--disable-dev-shm-usage','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{stdio:'ignore'});let ws;
 try{const active=path.join(profile,'DevToolsActivePort');for(let i=0;i<100&&!fs.existsSync(active);i++)await wait(100);const port=fs.readFileSync(active,'utf8').split('\n')[0];const tabs=await get('http://127.0.0.1:'+port+'/json/list');ws=new WS(tabs.find(t=>t.type==='page').webSocketDebuggerUrl);await new Promise(r=>ws.on('open',r));let seq=0;const pending=new Map(),errors=[];
 const call=(method,params={})=>new Promise((ok,bad)=>{pending.set(++seq,{ok,bad});ws.send(JSON.stringify({id:seq,method,params}));});
 ws.on('message',raw=>{const m=JSON.parse(raw);if(m.id){const h=pending.get(m.id);pending.delete(m.id);m.error?h.bad(Error(JSON.stringify(m.error))):h.ok(m.result);}else if(m.method==='Runtime.exceptionThrown')errors.push(m.params);});
 const ev=async expression=>{const r=await call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
 const shot=async name=>{const r=await call('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(out,name+'.png'),Buffer.from(r.data,'base64'));};
 await call('Runtime.enable');await call('Page.enable');await call('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false});await call('Page.navigate',{url:'http://127.0.0.1:'+server.address().port});
 for(let i=0;i<100&&!await ev('!!window.renderReport');i++)await wait(50);
 if(!await ev('!!window.renderReport'))throw Error('Module not loaded: '+JSON.stringify({errors,dom:await ev('({url:location.href,html:document.documentElement.outerHTML})'),module:await ev("import('/replay-report.js').then(()=> 'loaded').catch(e=>e.stack)")}));
 await ev('renderReport('+JSON.stringify(report)+')');await wait(200);
 assert.strictEqual(await ev('document.querySelectorAll(".rr-arm").length'),2);
 assert.strictEqual(await ev('document.querySelectorAll(".rr-joint").length'),14);
 assert.strictEqual(await ev('document.querySelector(".rr-details").open'),false);
 assert.strictEqual(await ev('document.querySelector("[data-rr-mode=error]").getAttribute("aria-pressed")'),'true');
 assert.strictEqual(await ev('document.querySelectorAll(".rr-error-line").length'),1);
 if(!process.env.F1_REPLAY_REPORT_FIXTURE)assert((await ev('document.querySelector(".rr-details").textContent')).includes('轨迹顺延 0.043 秒'));
 await shot('desktop');
 await ev('document.querySelectorAll("[data-rr-joint]")[12].click()');assert.strictEqual(await ev('document.querySelector("[data-r-channel]").value'),'12');
 await ev('document.querySelector("[data-rr-mode=trace]").click()');assert.strictEqual(await ev('document.querySelectorAll(".rr-original-line,.rr-current-line").length'),2);
 await ev('(()=>{let s=document.querySelector("[data-rr-scrubber]");s.value=Math.floor(Number(s.max)/2);s.dispatchEvent(new Event("input"));})()');
 assert((await ev('document.querySelector("[data-rr-scrubber]").getAttribute("aria-valuetext")')).includes('偏差'));
 await shot('trajectory');
 for(const width of [1920,1280]){await call('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});await wait(250);assert.strictEqual(await ev('document.documentElement.scrollWidth<=innerWidth'),true);assert(await ev('Math.abs(document.querySelector(".rr-chart").viewBox.baseVal.width-document.querySelector("[data-r-chart]").clientWidth)<2'));await shot('width-'+width);}
 await ev('document.querySelector(".rr-details").open=true');assert.strictEqual(await ev('document.querySelectorAll("[data-r-metrics] tbody tr").length'),report.comparison.channels.length);assert.strictEqual(await ev('document.documentElement.scrollWidth<=innerWidth'),true);
 const partial=JSON.parse(JSON.stringify(fake));partial.result.status='failed';partial.comparison.channels[0].metrics=null;partial.comparison.channels[0].points=[];partial.comparison.arms_summary.both.complete=false;partial.comparison.arms_summary.both.joints_compared=13;
 await ev('renderReport('+JSON.stringify(partial)+')');assert((await ev('document.querySelector(".rr-badge").textContent')).includes('仅已执行部分'));
 await ev('document.querySelector("[data-rr-joint]").click()');assert((await ev('document.querySelector("[data-r-chart]").textContent')).includes('没有可比较'));
 const gaps=JSON.parse(JSON.stringify(fake));gaps.comparison.channels[0].points=[[0,0,0],[1,null,0],[2,2,2.1]];
 await ev('renderReport('+JSON.stringify(gaps)+');document.querySelector("[data-rr-joint]").click()');assert.strictEqual(await ev('(document.querySelector(".rr-error-line").getAttribute("d").match(/M/g)||[]).length'),2);
 await ev('renderReport({result:{status:"failed"}})');assert((await ev('document.querySelector("#report").textContent')).includes('没有逐关节'));
 assert.deepStrictEqual(errors,[]);assert.strictEqual(writes,0);
 const result={result:'passed',fixture:process.env.F1_REPLAY_REPORT_FIXTURE?'recorded real report, read only':'synthetic',checks:['14 joints visible in two arm panels','default signed error chart','row selection and curve switch','time cursor values','full metrics preserved','1440/1920/1280 desktop no overflow','responsive chart labels','partial execution','missing feedback','gaps remain disconnected','legacy empty report','no JavaScript errors','zero write requests']};fs.writeFileSync(path.join(out,'report.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));
 }finally{if(ws)ws.terminate();chrome.kill('SIGTERM');server.closeAllConnections?.();server.close();setTimeout(()=>process.exit(process.exitCode||0),200);}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});

/* Sustained playback regression with real H.264 media and paced HTTP bytes.
 * Isolated loopback controller harness: no robot API or media property mocks.
 * NODE_PATH=/usr/share/nodejs node scripts/check_video_continuity_browser.cjs
 * Optional F1_VIDEO_CONTINUITY_OUTPUT, F1_VIDEO_CONTINUITY_WEB_ROOT,
 * F1_VIDEO_CONTINUITY_FIXTURE, F1_VIDEO_CONTINUITY_CASES (JSON), CHROME_BIN.
 */
const fs=require('fs'),http=require('http'),os=require('os'),path=require('path');
const cp=require('child_process'),crypto=require('crypto'),assert=require('assert'),WebSocket=require('ws');
const root=path.resolve(__dirname,'..');
const out=path.resolve(process.env.F1_VIDEO_CONTINUITY_OUTPUT||path.join(root,'results/video-continuity-fix-20261023/browser'));
const webRoot=path.resolve(process.env.F1_VIDEO_CONTINUITY_WEB_ROOT||path.join(root,'web'));
const duration=24,chunkBytes=16384,sleep=ms=>new Promise(r=>setTimeout(r,ms));
const report={isolated:true,robotActionsSent:0,realNetworkThrottle:true,mediaPropertyMocks:false,durationSeconds:duration,cases:[],blockedWrites:[],exceptions:[]};
let active,source,file,chrome,ws,server,profile,debugCall;
const sockets=new Set();
const html=`<!doctype html><html><body style="display:flex;margin:24px;gap:16px;background:#faf9f6">
<video data-camera="left" muted preload="metadata" width="400" src="/left.mp4"></video>
<video data-camera="head" muted preload="metadata" width="640" src="/head.mp4"></video>
<video data-camera="right" muted preload="metadata" width="400" src="/right.mp4"></video>
<script type="module">
import{SynchronizedVideos}from'/synced-video.js';
window.changes=[];window.mediaEvents=[];window.presented={};window.frameSamples=[];window.startedAt=null;
window.group=new SynchronizedVideos([...document.querySelectorAll('video')],{duration:24,onChange:s=>changes.push({...s,at:performance.now()})});
for(const v of group.videos){
 const frame=(at,m)=>{presented[v.dataset.camera]={time:m.mediaTime,at,presentedFrames:m.presentedFrames};v.requestVideoFrameCallback(frame);};
 v.requestVideoFrameCallback(frame);
 for(const type of ['waiting','playing','pause','seeking','seeked','canplay','ended','error'])v.addEventListener(type,()=>mediaEvents.push({camera:v.dataset.camera,type,at:performance.now(),time:v.currentTime}));
}
let observing=true;
const observe=at=>{if(!observing)return;if(startedAt!==null)frameSamples.push({at,time:group.time,phase:group.phase,intent:group.intent,videos:group.videos.map(v=>({camera:v.dataset.camera,time:v.currentTime,frame:presented[v.dataset.camera]||null,paused:v.paused,seeking:v.seeking,ready:v.readyState,buffer:[...Array(v.buffered.length)].map((_,i)=>[v.buffered.start(i),v.buffered.end(i)])}))});requestAnimationFrame(observe);};
requestAnimationFrame(observe);window.stopObserving=()=>{observing=false;};window.readyCase=new URL(location).searchParams.get('case');
</script></body></html>`;
function prepare(){
 fs.mkdirSync(out,{recursive:true});
 source=fs.readFileSync(path.join(webRoot,'synced-video.js'));
 report.sourceSha256=crypto.createHash('sha256').update(source).digest('hex');
 file=path.resolve(process.env.F1_VIDEO_CONTINUITY_FIXTURE||path.join(out,'continuity-fixture.mp4'));
 if(!fs.existsSync(file)){
  const encoded=cp.spawnSync(process.env.FFMPEG_BIN||'ffmpeg',['-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc2=size=640x360:rate=30','-t',String(duration),'-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p','-b:v','6M','-minrate','6M','-maxrate','6M','-bufsize','6M','-x264-params','nal-hrd=cbr:force-cfr=1','-g','15','-keyint_min','15','-sc_threshold','0','-movflags','+faststart','-an','-y',file],{encoding:'utf8',timeout:120000});
  assert.strictEqual(encoded.status,0,'FFmpeg fixture failed: '+(encoded.stderr||encoded.error));
 }
 const bytes=fs.statSync(file).size;assert(bytes>10*1024*1024,'Fixture too small to exercise sustained fetching.');
 report.fixture={file,bytes,averageBitsPerSecond:bytes*8/duration};
}
function serve(req,res){
 if(req.method!=='GET'){report.blockedWrites.push({method:req.method,url:req.url});res.writeHead(405);res.end();return;}
 const url=new URL(req.url,'http://127.0.0.1');
 if(url.pathname==='/synced-video.js'){res.setHeader('Content-Type','text/javascript');res.end(source);return;}
 if(!url.pathname.endsWith('.mp4')){res.setHeader('Content-Type','text/html');res.end(html);return;}
 const camera=path.basename(url.pathname,'.mp4'),size=fs.statSync(file).size,range=(req.headers.range||'').match(/^bytes=(\d+)-(\d*)$/);
 const start=range?Number(range[1]):0,end=range&&range[2]?Math.min(Number(range[2]),size-1):size-1;
 if(start>=size||end<start){res.writeHead(416,{'Content-Range':'bytes */'+size});res.end();return;}
 const entry={camera,start,end,bytes:0,atMs:Date.now(),chunks:[]};active.requests.push(entry);
 res.writeHead(range?206:200,{'Content-Type':'video/mp4','Accept-Ranges':'bytes','Content-Length':end-start+1,'Cache-Control':'no-store',...(range?{'Content-Range':`bytes ${start}-${end}/${size}`}:{})});
 if(camera!=='head'){
  const stream=fs.createReadStream(file,{start,end});stream.on('data',chunk=>{entry.bytes+=chunk.length;});
  res.on('close',()=>{entry.closedAtMs=Date.now();stream.destroy();});stream.pipe(res);return;
 }
 const fd=fs.openSync(file,'r'),speed=active.bytesPerSecond;let position=start,timer,closed=false;
 res.on('close',()=>{if(closed)return;closed=true;clearTimeout(timer);fs.closeSync(fd);entry.closedAtMs=Date.now();});
 const send=()=>{
  if(closed||res.destroyed)return;if(position>end){res.end();return;}
  const chunk=Buffer.allocUnsafe(Math.min(chunkBytes,end-position+1)),n=fs.readSync(fd,chunk,0,chunk.length,position);
  if(!n){res.end();return;}position+=n;entry.bytes+=n;entry.chunks.push({atMs:Date.now(),bytes:n});
  const schedule=()=>{timer=setTimeout(send,n/speed*1000);};if(res.write(chunk.subarray(0,n)))schedule();else res.once('drain',schedule);
 };send();
}
async function json(url){return new Promise((resolve,reject)=>http.get(url,r=>{let data='';r.on('data',c=>data+=c);r.on('end',()=>{try{resolve(JSON.parse(data));}catch(e){reject(e);}});}).on('error',reject));}
async function main(){
 prepare();server=http.createServer(serve);server.on('connection',s=>{sockets.add(s);s.on('close',()=>sockets.delete(s));});
 await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
 profile=fs.mkdtempSync(path.join(os.tmpdir(),'f1-video-continuity-'));
 chrome=cp.spawn(process.env.CHROME_BIN||'/usr/bin/google-chrome',['--headless=new','--no-sandbox','--no-proxy-server','--disable-background-networking','--password-store=basic','--disable-dev-shm-usage','--remote-debugging-port=0','--autoplay-policy=no-user-gesture-required','--log-net-log='+path.join(out,'chrome-netlog.json'),'--user-data-dir='+profile,'about:blank'],{stdio:'ignore'});
 const portFile=path.join(profile,'DevToolsActivePort');for(let i=0;i<100&&!fs.existsSync(portFile);i++)await sleep(100);
 assert(fs.existsSync(portFile),'Chrome did not start.');const port=fs.readFileSync(portFile,'utf8').split('\n')[0];
 const tabs=await json('http://127.0.0.1:'+port+'/json/list');ws=new WebSocket(tabs.find(t=>t.type==='page').webSocketDebuggerUrl);
 await new Promise((resolve,reject)=>{ws.once('open',resolve);ws.once('error',reject);});
 let id=0;const pending=new Map();ws.on('message',raw=>{const m=JSON.parse(raw);if(m.id&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);clearTimeout(p.timer);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result);}else if(m.method==='Runtime.exceptionThrown')report.exceptions.push(m.params.exceptionDetails);});
 const call=debugCall=(method,params={})=>new Promise((resolve,reject)=>{const n=++id,timer=setTimeout(()=>{pending.delete(n);reject(Error('Timed out: '+method));},45000);pending.set(n,{resolve,reject,timer});ws.send(JSON.stringify({id:n,method,params}));});
 const ev=async expression=>{const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
 await call('Runtime.enable');await call('Page.enable');await call('Emulation.setDeviceMetricsOverride',{width:1920,height:1080,deviceScaleFactor:1,mobile:false});
 const cases=JSON.parse(process.env.F1_VIDEO_CONTINUITY_CASES||'[{"name":"adequate-1x","bytesPerSecond":1048576,"speed":1,"maxWaits":1,"maxWallSeconds":35},{"name":"marginal-1x","bytesPerSecond":655360,"speed":1,"maxWaits":2,"maxWallSeconds":45},{"name":"adequate-2x","bytesPerSecond":2097152,"speed":2,"maxWaits":1,"maxWallSeconds":24},{"name":"severe-1x","bytesPerSecond":393216,"speed":1,"minWaits":2,"maxWaits":3,"maxWallSeconds":65}]');
 for(const config of cases){
  active={...config,requests:[]};report.cases.push(active);
  await call('Page.navigate',{url:'http://127.0.0.1:'+server.address().port+'/?case='+encodeURIComponent(config.name)});
  let loaded=false;for(let i=0;i<450;i++){if(await ev('window.readyCase==='+JSON.stringify(config.name))){loaded=true;break;}await sleep(100);}assert(loaded,'Controller did not load.');
  active.playWallMs=Date.now();await ev(`group.setRate(${config.speed});window.startedAt=performance.now();group.play()`);
  const deadline=Date.now()+(config.maxWallSeconds+5)*1000;let state;
  while(Date.now()<deadline){state=await ev('({time:group.time,phase:group.phase})');if(['ended','error'].includes(state.phase))break;await sleep(100);}
  active.wallSeconds=(Date.now()-active.playWallMs)/1000;active.final=state;
  Object.assign(active,await ev('stopObserving();({startedAt,changes,mediaEvents,frameSamples})'));
  await ev('group.dispose()');
  const playing=active.changes.find(v=>v.phase==='playing'&&v.at>=active.startedAt);active.firstStartSeconds=playing?(playing.at-active.startedAt)/1000:null;
  const seenPlaying={value:false};active.midstreamWaits=active.changes.filter((v,i)=>{if(v.at<active.startedAt)return false;if(v.phase==='playing')seenPlaying.value=true;return seenPlaying.value&&v.phase==='buffering'&&active.changes[i-1]?.phase!==v.phase;}).map(v=>({at:v.at,time:v.time}));
  const samples=active.frameSamples.filter(v=>playing&&v.at>=playing.at&&v.videos.length===3&&v.videos.every(x=>x.frame));
  active.recoveries=active.midstreamWaits.map(wait=>{
   const resumed=active.changes.find(v=>v.phase==='playing'&&v.at>wait.at);
   const sample=resumed&&active.frameSamples.find(v=>v.at>=resumed.at),head=sample?.videos.find(v=>v.camera==='head');
   const buffer=head?.buffer.find(range=>range[0]<=head.time+.015&&range[1]>=head.time);
   return {time:wait.time,waitSeconds:resumed?(resumed.at-wait.at)/1000:null,
    headBufferedSeconds:buffer?buffer[1]-head.time:null,remainingSeconds:head?duration-head.time:null};
  });
  active.maxPresentedSkewSeconds=samples.length?Math.max(...samples.map(v=>Math.max(...v.videos.map(x=>x.frame.time))-Math.min(...v.videos.map(x=>x.frame.time)))):null;
  const firstWall=active.playWallMs+(active.firstStartSeconds||0)*1000;
  active.headBytesAtFirstStart=active.requests.filter(v=>v.camera==='head').flatMap(v=>v.chunks).filter(v=>v.atMs<=firstWall).reduce((n,v)=>n+v.bytes,0);
  active.headFractionAtFirstStart=active.headBytesAtFirstStart/report.fixture.bytes;
  active.deliveredHeadBytes=active.requests.filter(v=>v.camera==='head').reduce((n,v)=>n+v.bytes,0);
  fs.writeFileSync(path.join(out,config.name+'.json'),JSON.stringify(active,null,2));
  const summary={name:config.name,wallSeconds:active.wallSeconds,firstStartSeconds:active.firstStartSeconds,midstreamWaits:active.midstreamWaits.length,maxPresentedSkewSeconds:active.maxPresentedSkewSeconds,headFractionAtFirstStart:active.headFractionAtFirstStart,final:state};
  console.log(JSON.stringify(summary));
  assert.strictEqual(state.phase,'ended',config.name+': complete real video must finish.');
  assert(state.time>=duration-.02,config.name+': timeline did not reach the end.');
  assert(samples.length>100,config.name+': insufficient rendered-frame evidence.');
  assert(active.maxPresentedSkewSeconds<=.1001,config.name+': actual presented frame drift exceeded 100 ms.');
  assert(active.midstreamWaits.length>=(config.minWaits||0),config.name+': fixture did not exercise repeated real depletion.');
  assert(active.midstreamWaits.length<=config.maxWaits,config.name+': frequent stalls persist ('+active.midstreamWaits.length+').');
  assert(active.firstStartSeconds<10,config.name+': startup wait is excessive.');
  assert(active.headFractionAtFirstStart<.5,config.name+': startup must not predownload most/all video.');
  assert(active.wallSeconds<=config.maxWallSeconds,config.name+': full playback took too long.');
 }
 assert.strictEqual(report.blockedWrites.length,0,'Unexpected write request.');assert.strictEqual(report.exceptions.length,0,'Unexpected browser exception.');report.passed=true;
}
main().catch(async error=>{report.passed=false;report.failure=error.stack;process.exitCode=1;console.error(error.stack);if(debugCall)try{const r=await debugCall('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(out,'failure.png'),Buffer.from(r.data,'base64'));}catch{}}).finally(async()=>{
 fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'summary.json'),JSON.stringify(report,null,2));
 console.log(JSON.stringify({passed:report.passed,sourceSha256:report.sourceSha256,output:out,failure:report.failure}));ws?.close();chrome?.kill();for(const socket of sockets)socket.destroy();server?.close();await sleep(500);if(profile)try{fs.rmSync(profile,{recursive:true,force:true});}catch{}
});

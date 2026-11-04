/* One transport clock and one buffering barrier for all recorded cameras.
 * This controller only plays media; it never sends robot commands. */
const FRAME_EPS=.06, SYNC_EPS=.08;
const START_BUFFER=1, SOURCE_LOW=.15;
const finite=Number.isFinite;
export class SynchronizedVideos {
 constructor(videos,{time=0,duration=0,onChange=()=>{}}={}){
  this.videos=videos;this.time=time;this.duration=duration;this.fixedDuration=duration>0;this.onChange=onChange;
  this.rate=1;this.intent=false;this.phase='paused';this.reason='';this.generation=0;
  this.frames=new Map();this.frameCallbacks=new Map();this.targets=new Map();this.events=[];
  this.resumePositions=new Map();this.bufferSeconds=START_BUFFER;this.starvations=0;
  this.barrier=false;this.disposed=false;this.raf=null;this.seekTimer=null;this.lastTick=0;this.lastNotify=0;
  for(const video of videos){
   this.on(video,'loadedmetadata',()=>{this.refreshDuration();this.wake();});
   for(const event of ['loadeddata','canplay','progress','seeked','ended'])this.on(video,event,()=>this.wake());
   for(const event of ['waiting','seeking'])this.on(video,event,()=>{
    // A decoded-queue refill can emit waiting even with source bytes available.
    // Let it finish; actual frame skew/staleness remains monitored below.
    const starved=event==='waiting'&&this.sourceLow(video);
    const blocked=event==='seeking'?video.seeking:(video.readyState<2||starved);
    if(blocked&&this.intent&&this.participants().includes(video)&&['playing','starting'].includes(this.phase))this.wait('buffering',starved?'source':'sync');
   });
   this.on(video,'error',()=>this.fail('有一路视频无法读取，请重新打开录制后重试。'));
   if(video.requestVideoFrameCallback)this.watchFrame(video);
  }
  this.refreshDuration();if(time>0)this.seek(time);else this.notify(true);
 }
 on(video,event,handler){video.addEventListener(event,handler);this.events.push([video,event,handler]);}
 offset(video){return Number(video.dataset.offset||0);}
 end(video){return finite(video.duration)?this.offset(video)+video.duration:Infinity;}
 participants(time=this.time){return this.videos.filter(v=>time+1e-6>=this.offset(v)&&time<this.end(v)-.002);}
 local(video){return Math.max(0,Math.min(finite(video.duration)?Math.max(0,video.duration-.001):Infinity,this.time-this.offset(video)));}
 refreshDuration(){if(!this.fixedDuration){const ends=this.videos.map(v=>this.end(v)).filter(finite);if(ends.length)this.duration=Math.max(...ends);} }
 setDuration(duration){if(finite(duration)&&duration>0){this.duration=duration;this.fixedDuration=true;this.wake();}}
 watchFrame(video){
  const callback=(now,metadata)=>{
   if(this.disposed)return;
   this.frames.set(video,{time:metadata.mediaTime,at:now});
   this.frameCallbacks.set(video,video.requestVideoFrameCallback(callback));this.wake();
  };
  this.frameCallbacks.set(video,video.requestVideoFrameCallback(callback));
 }
 frameTime(video){return this.frames.get(video)?.time??(video.requestVideoFrameCallback?null:video.currentTime);}
 notify(force=false){
  const now=performance.now();if(!force&&now-this.lastNotify<100)return;this.lastNotify=now;
  this.onChange({time:this.time,intent:this.intent,phase:this.phase,reason:this.reason});
 }
 wake(){if(this.disposed||this.raf!==null)return;this.raf=requestAnimationFrame(now=>{this.raf=null;this.tick(now);});}
 pauseVideos(){this.videos.forEach(v=>v.pause());}
 wait(reason='buffering',cause='sync'){
  // Repeated waiting/seeked events never move the frozen target or restart seeks.
  if(this.barrier)return;
  this.resumePositions.clear();
  if(this.phase==='playing'){
   const active=this.participants(),shown=active.map(v=>this.frameTime(v)===null?null:this.frameTime(v)+this.offset(v));
   if(shown.length&&shown.every(finite)&&Math.max(...shown)-Math.min(...shown)<=SYNC_EPS)this.time=Math.min(this.duration,Math.max(this.time,Math.min(...shown)));
   // An aligned group can wait where it stopped. Media clocks normally sit
   // between displayed frames; seeking back on every refill restarts decoders.
   const clocks=active.map(v=>v.currentTime+this.offset(v));
   if(active.length&&shown.every(finite)&&Math.max(...shown)-Math.min(...shown)<=FRAME_EPS&&Math.max(...clocks)-Math.min(...clocks)<=.025&&active.every((v,i)=>!v.seeking&&clocks[i]>=shown[i]-.015&&clocks[i]-shown[i]<=FRAME_EPS)){
    active.forEach(v=>this.resumePositions.set(v,v.currentTime));
   }
  }
  if(cause==='source'){this.starvations++;this.bufferSeconds=Math.min(10,3*2**Math.min(2,this.starvations-1));}
  else this.bufferSeconds=START_BUFFER;
  this.generation++;this.barrier=true;this.phase=reason;this.reason='';this.targets.clear();
  this.pauseVideos();this.lastTick=0;this.notify(true);this.wake();
 }
 play(){
  if(this.disposed||!this.videos.length)return;
  if(this.time>=this.duration-.02)this.seek(0);
  this.intent=true;this.videos.forEach(v=>v.preload='auto');
  if(!this.barrier)this.wait('buffering');else this.wake();this.notify(true);
 }
 pause(){
  this.intent=false;this.generation++;clearTimeout(this.seekTimer);this.seekTimer=null;
  this.pauseVideos();this.phase='paused';this.lastTick=0;
  if(this.barrier)this.wake();this.notify(true);
 }
 seek(value,{defer=false}={}){
  if(this.disposed)return;
  clearTimeout(this.seekTimer);this.seekTimer=null;this.generation++;
  this.pauseVideos();this.time=Math.max(0,Math.min(this.duration||Infinity,finite(value)?value:0));
  this.barrier=true;this.phase='seeking';this.targets.clear();this.resumePositions.clear();this.bufferSeconds=START_BUFFER;this.lastTick=0;this.notify(true);
  if(defer)this.seekTimer=setTimeout(()=>{this.seekTimer=null;this.wake();},100);
  this.wake();
 }
 setRate(value){if(!finite(value)||value<=0)return;this.rate=value;this.videos.forEach(v=>v.playbackRate=value);if(this.intent)this.wait('buffering');}
 bufferAhead(video,target=video.currentTime){
  for(let i=0;i<video.buffered.length;i++)if(video.buffered.start(i)<=target+.015&&video.buffered.end(i)>=target)return video.buffered.end(i)-target;
  return 0;
 }
 sourceLow(video){return video.currentTime<video.duration-.05&&this.bufferAhead(video)<SOURCE_LOW*this.rate;}
 hasBuffer(video,target){
  if(!this.intent)return true;
  const end=Math.min(video.duration-.002,target+this.bufferSeconds*this.rate);
  for(let i=0;i<video.buffered.length;i++)if(video.buffered.start(i)<=target+.015&&video.buffered.end(i)>=end)return true;
  return false;
 }
 align(video){
  if(video.readyState<1||!finite(video.duration)||video.seeking)return false;
  const target=this.resumePositions.get(video)??this.local(video),frame=this.frameTime(video);
  if(Math.abs(video.currentTime-target)>.025||frame===null||Math.abs(frame-target)>FRAME_EPS){
   if(this.targets.get(video)!==target){
    this.targets.set(video,target);
    try{video.currentTime=target;}catch{this.fail('视频定位失败，请重新打开录制后重试。');}
   }
   return false;
  }
  const needed=this.intent&&target<video.duration-.05?3:2;
  return video.readyState>=needed&&this.hasBuffer(video,target);
 }
 async start(active){
  const generation=++this.generation;this.barrier=false;this.phase='starting';this.playingSet=new Set(active);this.notify(true);
  try{
   await Promise.all(active.map(v=>{v.playbackRate=this.rate;return v.play();}));
   if(this.disposed||!this.intent){this.pauseVideos();return;}
   if(generation!==this.generation||this.phase!=='starting')return;
   this.phase='playing';this.lastTick=0;this.notify(true);this.wake();
  }catch(error){
   if(generation!==this.generation||this.disposed)return;
   if(error.name==='NotAllowedError')this.fail('浏览器未允许播放，请再次点击播放。');
   else if(error.name==='AbortError'){if(this.intent)this.wait('buffering');}
   else this.fail('视频播放失败，请重新打开录制后重试。');
  }
 }
 tick(now){
  if(this.disposed)return;
  const active=this.participants();
  if(active.some(v=>v.error)){this.fail('有一路视频无法读取，请重新打开录制后重试。');return;}
  if(this.barrier){
   if(this.seekTimer===null){
    // Visit every camera even when an earlier camera is not ready.
    const aligned=this.videos.map(v=>({video:v,ready:this.align(v)})).filter(item=>active.includes(item.video)).map(item=>item.ready);
    if(aligned.every(Boolean)){
     if(this.intent)this.start(active);
     else{this.barrier=false;this.phase='paused';this.notify(true);}
    }
   }
  }else if(this.intent&&this.phase==='playing'){
   if(active.some(v=>!this.playingSet.has(v))){this.wait('buffering');return;}
   const moving=active.filter(v=>!v.ended);
   const times=moving.map(v=>v.currentTime+this.offset(v));
   const frames=moving.map(v=>this.frameTime(v));
   const presented=frames.map((t,i)=>t===null?null:t+this.offset(moving[i]));
   const stale=moving.some((v,i)=>v.seeking||v.readyState<2||(!v.ended&&v.paused)||presented[i]===null||times[i]-presented[i]>Math.max(.1,.15*this.rate));
   const skew=presented.length?Math.max(...presented)-Math.min(...presented):0;
   // Some codecs need several subsequent packets before the next frame can
   // decode. Once presentation really stalls, a small nonempty byte buffer is
   // still a shortage; give it recovery reserve instead of repeatedly retrying.
   if(stale||skew>SYNC_EPS){this.wait('buffering',moving.some(v=>v.readyState<3&&this.bufferAhead(v)<START_BUFFER*this.rate)?'source':'sync');return;}
   if(presented.length)this.time=Math.min(this.duration,Math.max(this.time,Math.min(...presented)));
   else if(active.length)this.time=Math.min(this.duration,Math.max(this.time,Math.min(...active.map(v=>this.end(v)))));
   else if(this.lastTick)this.time=Math.min(this.duration,this.time+Math.min(.05,(now-this.lastTick)/1000)*this.rate);
   this.lastTick=now;
   if(this.time>=this.duration-.015){this.time=this.duration;this.intent=false;this.phase='ended';this.pauseVideos();this.notify(true);return;}
  }
  this.notify();if(this.intent||this.barrier)this.wake();
 }
 fail(message){this.reason=message;this.intent=false;this.barrier=false;this.generation++;this.phase='error';this.pauseVideos();this.notify(true);}
 dispose(){
  this.disposed=true;this.intent=false;this.generation++;clearTimeout(this.seekTimer);cancelAnimationFrame(this.raf);this.pauseVideos();
  for(const [video,id] of this.frameCallbacks)video.cancelVideoFrameCallback?.(id);
  this.events.forEach(([v,event,handler])=>v.removeEventListener(event,handler));
 }
}

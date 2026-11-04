/* Real browser rendering of the existing HIL workspace with isolated status
 * fixtures. No robot API, receiver, recorder or command publisher is used. */
const fs=require('fs'),path=require('path'),http=require('http'),os=require('os');
const cp=require('child_process'),assert=require('assert'),WebSocket=require('ws');
const root=path.resolve(__dirname,'..');
const output=process.env.HIL_RECOVERY_OUTPUT||path.join(root,'results/hil-recovery-20261023/browser');
fs.mkdirSync(output,{recursive:true});
const fixture={active:true,summary_fresh:true,summary_age_s:0,source:'structured_snapshot',
 phase:'input_wait_release_rb',can_finish:true,run_id:'same-episode',
 session_key:{run_id:'process',pid:123,start_ticks:'456'},events:[],
 status:{purpose:'demonstration',execution:'POWERED',input_fresh:true,input_paused:true,
  side:'right',rb:false,recovery_reason:'manual_candidate_collision',
  recovery_required:'release_neutral_rb',camera_age_ms:{head:20,left_wrist:20,right_wrist:20},
  recording:{phase:'recording',completed:321}}};
const styles=fs.readFileSync(path.join(root,'web/index.html'),'utf8').match(/<link rel="stylesheet"[^>]+>/g).join('\n').replace(/href="\//g,'href="file://'+root+'/web/');
const harness=`<!doctype html><meta charset="utf-8">${styles}<body data-page="hil"><div class="app-shell"><aside class="sidebar">F1-infra<br>HIL状态测试</aside><div class="workspace"><main id="mount"></main></div></div>
<script type="module">
import {HILPanel} from 'file://${root}/web/hil.js';
const originalFetch=window.fetch;window.fetch=(url,options)=>originalFetch(String(url).startsWith('./assets/')?'file://${root}/web/'+String(url).slice(2):url,options);
const escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
window.apiCalls=[];
const p=new HILPanel({api:(...args)=>{window.apiCalls.push(args);throw Error('Unexpected API');},esc:escape,toast:()=>{},login:()=>{throw Error('Unexpected login')}});
window.setFixture=data=>{p.data=data;p.online=true;p.received=performance.now();p.render();};
window.mountRoute=route=>{p.dispose();location.hash='hil?route='+route;document.querySelector('#mount').innerHTML=p.html();p.routes=[{id:route,title:{demonstration:'人工示教',play:'自由接入',policy:'策略 HIL'}[route],purpose:{demonstration:'demonstration',play:'teleop_play',policy:'policy_hil'}[route],ready:true,commands:[]}];p.data=${JSON.stringify(fixture)};p.online=true;p.received=performance.now();p.mount();p.render();};
window.mountRoute('demonstration');window.ready=true;
</script>`;
let writes=0;const browserErrors=[];
const wait=ms=>new Promise(r=>setTimeout(r,ms));
const json=url=>new Promise((resolve,reject)=>http.get(url,r=>{let text='';r.on('data',b=>text+=b);r.on('end',()=>resolve(JSON.parse(text)));}).on('error',reject));
(async()=>{
 const profile=fs.mkdtempSync(path.join(os.tmpdir(),'hil-recovery-browser-'));
 const harnessPath=path.join(output,'harness.html');fs.writeFileSync(harnessPath,harness);
 const chrome=cp.spawn('/usr/bin/google-chrome',['--headless=new','--allow-file-access-from-files','--no-sandbox','--no-proxy-server','--disable-dev-shm-usage','--enable-unsafe-swiftshader','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{stdio:'ignore'});
 let ws;
 try{
  const active=path.join(profile,'DevToolsActivePort');for(let i=0;i<100&&!fs.existsSync(active);i++)await wait(100);
  const port=fs.readFileSync(active,'utf8').split('\n')[0];
  ws=new WebSocket((await json('http://127.0.0.1:'+port+'/json/list')).find(x=>x.type==='page').webSocketDebuggerUrl);
  await new Promise(r=>ws.on('open',r));let id=0;const pending=new Map(),errors=browserErrors;
  ws.on('message',raw=>{const m=JSON.parse(raw);if(m.id&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result);}else if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails);else if(m.method==='Network.requestWillBeSent'&&m.params.request.method!=='GET')writes++;});
  const call=(method,params={})=>new Promise((resolve,reject)=>{const n=++id;const timer=setTimeout(()=>reject(Error('CDP timeout '+method)),10000);pending.set(n,{resolve:r=>{clearTimeout(timer);resolve(r)},reject:e=>{clearTimeout(timer);reject(e)}});ws.send(JSON.stringify({id:n,method,params}));});
  const ev=async expression=>{const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
  await call('Runtime.enable');await call('Page.enable');await call('Network.enable');
  await call('Page.navigate',{url:'file://'+harnessPath+'#hil?route=demonstration'});
  for(let i=0;i<100&&!await ev('window.ready===true');i++)await wait(100);
  assert(await ev('window.ready===true'),'HIL harness ready');
  const states=[],sizes=[];
  for(const [width,height] of [[1280,800],[1440,900],[1920,1080]]){
   await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
   for(const [route,purpose] of [['demonstration','demonstration'],['play','teleop_play'],['policy','policy_hil']]){
    await ev('mountRoute('+JSON.stringify(route)+')');
    for(const phase of ['input_wait_reconnect','input_wait_release_rb','input_wait_fresh_packet','input_wait_press_rb','input_wait_feedback','input_wait_safe_anchor','input_wait_update_sender']){
     const status={...fixture.status,purpose,recording:purpose==='teleop_play'?{}:fixture.status.recording};
     await ev('setFixture('+JSON.stringify({...fixture,phase,status})+')');
     const text=await ev("document.querySelector('#hil-overview').textContent");
     assert(text.includes('不要按 START 恢复'),phase);
     assert(text.includes('碰撞风险'),phase);
     if(purpose==='teleop_play')assert(text.includes('自由接入不录制')&&!text.includes('同一条仍在录制'));
     else assert(text.includes('同一条仍在录制'));
     if(phase==='input_wait_reconnect')assert(text.includes('等待 sender 和手柄重新连接'));
     if(phase==='input_wait_safe_anchor')assert(text.includes('当前实测姿态未通过检查'));
     if(route!=='policy'){
      await ev("document.querySelector('[data-hil-tab=diagnostic]').click()");
      assert((await ev("document.querySelector('#hil-persistent-status').textContent")).includes('不要按 START 恢复'));
      await ev("document.querySelector('[data-hil-tab=live]').click()");
     }
     states.push({width,route,phase});
    }
    await ev('setFixture('+JSON.stringify({...fixture,status:{...fixture.status,purpose}})+')');
    assert(await ev('document.documentElement.scrollWidth<=innerWidth'),'horizontal overflow');
    const shot=await call('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(output,route+'-'+width+'.png'),Buffer.from(shot.data,'base64'));sizes.push({width,height,route});
   }
  }
  await ev("mountRoute('demonstration')");
  for(const recordPhase of ['saving','idle']){
   await ev('setFixture('+JSON.stringify({...fixture,phase:'safe_hold',status:{...fixture.status,input_paused:false,recovery_reason:null,hold_reason:'feedback_stale',recording:{phase:recordPhase,completed:321}}})+')');
   const text=await ev("document.querySelector('#hil-overview').textContent");assert(text.includes('START 只会开新条'));assert(!text.includes('同一条仍在录制'));
  }
  await ev("mountRoute('policy')");
  await ev('setFixture('+JSON.stringify({...fixture,phase:'safe_hold',status:{...fixture.status,purpose:'policy_hil',input_paused:false,recovery_reason:null,hold_reason:'policy_failed:timeout'}})+')');
  assert((await ev("document.querySelector('#hil-overview').textContent")).includes('START 转人工接管'));
  await ev("mountRoute('demonstration')");
  await ev('setFixture('+JSON.stringify({...fixture,phase:'hil_latched',status:{...fixture.status,input_paused:false,recovery_reason:null,limit_reason:'joint boundary'}})+')');
  assert((await ev("document.querySelector('#hil-overview').textContent")).includes('改向限位内移动'));
  await ev("document.querySelector('[data-hil-tab=teaching]').click();document.querySelector('[data-lesson=rb]').dispatchEvent(new MouseEvent('click',{bubbles:true}))");
  assert((await ev("document.querySelector('#hil-lesson-description').textContent")).includes('START 不负责恢复'));
  assert.equal(await ev('apiCalls.length'),0);assert.equal(writes,0);assert.deepEqual(errors,[]);
  const result={ok:true,status_cases:states.length,states,sizes,errors,api_calls:0,network_writes:writes};
  fs.writeFileSync(path.join(output,'review.json'),JSON.stringify(result,null,2));console.log(JSON.stringify({ok:true,status_cases:states.length,desktop_screenshots:sizes.length,errors:0,network_writes:writes}));
 }finally{if(ws)ws.close();chrome.kill();}
})().catch(e=>{console.error(e);console.error(JSON.stringify({browserErrors}));process.exitCode=1;});

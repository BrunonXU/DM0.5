/* Latest compressed frames only. Preview never controls acquisition. */
export function cameraHTML(title='实时视野',caption='三路相机') {
 return `<details open class="camera-section" data-camera-preview><summary class="camera-section-heading"><h2>${title}</h2><span class="live-label">${caption} · 收起暂停</span></summary><div class="camera-grid">${['left','head','right'].map((key,i)=>`<figure class="camera-card" data-live-camera="${key}"><div class="camera-placeholder camera-stale" data-camera="${key}"><img alt="${['左腕','头部','右腕'][i]}实时相机" hidden><span class="camera-overlay">等待图像</span></div><figcaption class="camera-title"><span>${['左腕相机','头部 · 主视角','右腕相机'][i]}</span><span data-camera-age="${key}">等待图像</span></figcaption></figure>`).join('')}</div></details>`;
}
export class CameraPreview {
 constructor(root,{fps=10}={}) {
  this.root=root;this.period=1000/fps;this.disposed=false;this.visible=false;
  this.rows=[...root.querySelectorAll('[data-camera]')].map(el=>({el,key:el.dataset.camera,img:el.querySelector('img'),overlay:el.querySelector('.camera-overlay'),age:root.querySelector(`[data-camera-age="${el.dataset.camera}"]`),controller:null,next:0,url:null,frame:null,received:null,stamps:[]}));
  this.visibility=()=>this.sync();document.addEventListener('visibilitychange',this.visibility);root.addEventListener('toggle',this.visibility);
  this.observer=new IntersectionObserver(entries=>{this.visible=entries.some(e=>e.isIntersecting);this.sync();});this.observer.observe(root);
  this.timer=setInterval(()=>this.tick(),25);
 }
 active(){return !this.disposed&&this.root.isConnected&&this.visible&&!document.hidden&&this.root.open;}
 sync(){if(!this.active())for(const row of this.rows)row.controller?.abort();else this.tick();}
 tick(){if(!this.active())return;const now=performance.now();for(const row of this.rows){this.paint(row,now);if(!row.controller&&now>=row.next)this.request(row);}}
 paint(row,now){const age=row.received===null?Infinity:(now-row.received)/1000;row.el.classList.toggle('camera-stale',age>2);row.overlay.hidden=age<=2;row.overlay.textContent=row.received===null?'等待图像':'画面失鲜';row.stamps=row.stamps.filter(t=>now-t<5000);const fps=row.stamps.length>1?(row.stamps.length-1)*1000/(row.stamps.at(-1)-row.stamps[0]):0;row.age.textContent=Number.isFinite(age)?`${age>.5?'延迟 · ':''}${age.toFixed(1)}s · ${fps.toFixed(1)}fps`:'等待图像';}
 async request(row){const start=performance.now(),controller=new AbortController();row.controller=controller;row.next=start+this.period;const timeout=setTimeout(()=>controller.abort(),1500);try{
   const r=await fetch(`/api/cameras/${row.key}.jpg?t=${Date.now()}`,{cache:'no-store',signal:controller.signal});if(!r.ok)throw Error('camera unavailable');
   const frame=r.headers.get('X-F1-Frame-Id'),rawAge=r.headers.get('X-F1-Frame-Age-Ms');if(!frame||rawAge===null)throw Error('camera metadata missing');const age=Number(rawAge);if(!Number.isFinite(age)||age<0)throw Error('camera age invalid');const blob=await r.blob();
   if(!this.active()||controller.signal.aborted)return;
   // Repeated frame identity never advances receive time or measured FPS.
   if(frame!==row.frame){const url=URL.createObjectURL(blob),previous=row.url;row.img.src=url;row.img.hidden=false;row.url=url;if(previous)URL.revokeObjectURL(previous);row.frame=frame;row.received=start-age;row.stamps.push(performance.now());}
   this.paint(row,performance.now());
  }catch(e){if(e.name!=='AbortError'){row.next=performance.now()+500;this.paint(row,performance.now());}}finally{clearTimeout(timeout);if(row.controller===controller)row.controller=null;}
 }
 dispose(){this.disposed=true;clearInterval(this.timer);this.observer.disconnect();document.removeEventListener('visibilitychange',this.visibility);this.root.removeEventListener('toggle',this.visibility);for(const row of this.rows){row.controller?.abort();if(row.url)URL.revokeObjectURL(row.url);}}
}

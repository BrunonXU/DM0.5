// Read only original head frames. Covers are disposable; annotations are not.
export function coverURL(episode) {
 const match=/^\/api\/data\/(hil|capture)\/([a-f0-9]{24})\/timeline$/.exec(episode.replay_url||'');
 if(!match||match[2]!==episode.id||!episode.source_signature)return null;
 return `/api/data/${match[1]}/${match[2]}/head.jpg?t=0&source=${encodeURIComponent(episode.source_signature)}`;
}

export class DataCovers {
 constructor(){this.cache=new Map();this.queue=[];this.active=0;this.observer=new IntersectionObserver(entries=>{for(const entry of entries)if(entry.isIntersecting){this.observer.unobserve(entry.target);this.show(entry.target);}},{rootMargin:'100px'});}
 mount(root,episodes){
  this.observer.disconnect();const rows=new Map(episodes.map(e=>[e.id,e]));
  for(const node of root.querySelectorAll('[data-cover]')){
   const row=rows.get(node.dataset.cover),url=row&&coverURL(row);
   if(node._coverPlaceholder===undefined)node._coverPlaceholder=node.innerHTML;
   if(node.dataset.coverUrl!==url){node.innerHTML=node._coverPlaceholder;node.classList.remove('has-cover');delete node.dataset.coverUrl;}
   if(!url)continue;
   if(node.dataset.coverUrl===url&&node.querySelector('img'))continue;
   node.dataset.coverUrl=url;this.observer.observe(node);
  }
 }
 async show(node){
  if(!node.isConnected)return;const url=node.dataset.coverUrl;
  try{
   const source=await this.load(url,node);
   if(!source||!node.isConnected||node.dataset.coverUrl!==url)return;
   const img=new Image();img.alt='';img.draggable=false;img.decoding='async';img.src=source;await img.decode();
   if(node.isConnected&&node.dataset.coverUrl===url){node.replaceChildren(img);node.classList.add('has-cover');}
  }catch{/* Keep the camera placeholder when an original frame is unavailable. */}
 }
 load(url,node){
  const found=this.cache.get(url);if(found&&found.expires>Date.now())return found.promise;
  if(found)this.cache.delete(url);
  const item={expires:Infinity};item.promise=new Promise(resolve=>this.queue.push({url,node,resolve,item}));this.cache.set(url,item);
  while(this.cache.size>48)this.cache.delete(this.cache.keys().next().value);
  this.drain();return item.promise;
 }
 drain(){
  while(this.active<2&&this.queue.length){
   const job=this.queue.shift();if(!job.node.isConnected){this.cache.delete(job.url);job.resolve(null);continue;}
   this.active++;this.read(job.url).then(job.resolve,()=>{job.item.expires=Date.now()+60000;job.resolve(null);}).finally(()=>{this.active--;this.drain();});
  }
 }
 async read(url){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);
  try{
   const response=await fetch(url,{signal:controller.signal,credentials:'same-origin'});
   if(!response.ok)throw new Error('cover unavailable');
   const blob=await response.blob();if(!blob.type.startsWith('image/')||blob.size>8*1024*1024)throw new Error('invalid cover');
   return await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=reject;reader.readAsDataURL(blob);});
  }finally{clearTimeout(timer);}
 }
}

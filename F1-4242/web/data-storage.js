// Read-only presentation of existing catalogue and disk-health snapshots.
// Recording counts exclude derived datasets (their segments are not new takes).
const valid = n => typeof n === 'number' && Number.isFinite(n) && n >= 0;
export function storageSummary(catalog, disk) {
 const categories=[{id:'lxl',name:'LXL',detail:'Pico 录制',bytes:0,count:0},{id:'rollout',name:'真机Rollout',detail:'策略独立评测',bytes:0,count:0},{id:'hil',name:'HIL',detail:'人工示教 · DAgger · RLT',bytes:0,count:0}];
 const modes={demonstration:0,dagger:0,rlt:0};
 const seen=new Set();let missingSizes=false;
 for(const row of (catalog && catalog.episodes)||[]){
  const key=row.path||row.id;if(!key||seen.has(key))continue;seen.add(key);
  const mode=row.capture_mode||(['rlt','rollout','shadow','unknown'].includes(row.recording_kind)?row.recording_kind:row.source==='hil_demonstration'?'demonstration':row.source==='hil_policy_hil'?'dagger':null);
  const bucket=row.source==='unified_dataset'?null:mode==='rollout'?categories[1]:['lxl','pico_capture','lerobot'].includes(row.source)?categories[0]:Object.prototype.hasOwnProperty.call(modes,mode)?categories[2]:null;
  if(bucket===categories[2])modes[mode]++;
  if(!bucket)continue;
  bucket.count++;if(valid(row.size_bytes))bucket.bytes+=row.size_bytes;else missingSizes=true;
 }
 const total=categories.reduce((n,c)=>n+c.bytes,0),count=categories.reduce((n,c)=>n+c.count,0);
 const available=!!catalog&&Array.isArray(catalog.episodes),partial=!!(catalog&&(catalog.error||catalog.available===false))||missingSizes;
 const capacity=disk&&valid(disk.total_bytes)&&disk.total_bytes>0&&valid(disk.free_bytes)&&valid(disk.used_bytes)&&disk.used_bytes+disk.free_bytes<=disk.total_bytes+4096
  ?{total:disk.total_bytes,free:disk.free_bytes,used:disk.used_bytes,reserved:Math.max(0,disk.total_bytes-disk.used_bytes-disk.free_bytes)}:null;
 return {categories,modes,total,count,available,partial,capacity};
}
const gb=n=>n===0?'0 GB':n<1e7?'<0.01 GB':`${(n/1e9).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})} GB`;
export function storageHTML(catalog,disk,{stale=false}={}){
 const s=storageSummary(catalog,disk),d=s.capacity;
 const colors={lxl:'#d7ad59',hil:'#688bc0',rollout:'#86a58e',other:'#959ba5',free:'#eeece7'};
 // Flat segments retain the actual capacity scale; unknown capacity stays unknown.
 const other=d?Math.max(0,d.total-d.free-s.total):0;
 const segments=d?[...s.categories,{id:'other',bytes:other},{id:'free',bytes:d.free}]:[];
 const graphic=`<div class="storage-bar" role="img" aria-label="${d?'磁盘容量分布':'磁盘容量尚未读取'}">${segments.map(c=>`<span style="flex:${c.bytes};background:${colors[c.id]}"></span>`).join('')}</div>`;
 const rows=s.categories.map(c=>`<div class="storage-category" title="${c.id==='hil'?`人工示教 ${s.modes.demonstration} 条 · DAgger ${s.modes.dagger} 条 · RLT ${s.modes.rlt} 条`:c.detail}"><span class="storage-label"><i style="background:${colors[c.id]}"></i>${c.name}</span><strong>${s.available?gb(c.bytes):'—'}<small>${s.available?c.count:'—'} 条</small></strong></div>`).join('');
 return `<div class="storage-heading"><h2>存储</h2><span>${stale?'上次读取':s.partial?'部分目录未读取':d?`共 ${gb(d.total)}`:''}</span></div><div class="storage-layout">${graphic}<div class="storage-legend">${rows}<div class="storage-other" title="含系统文件、导出数据、缓存和系统预留空间"><span class="storage-label"><i style="background:${colors.other}"></i>系统及其他</span><span>${d?gb(other):'—'}</span></div><div class="storage-remaining"><span>剩余空间</span><strong class="storage-free">${d?gb(d.free):'—'}</strong></div></div></div>`;
}

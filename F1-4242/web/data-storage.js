// Read-only presentation of existing catalogue and disk-health snapshots.
// Recording counts exclude derived datasets (their segments are not new takes).
const valid = n => typeof n === 'number' && Number.isFinite(n) && n >= 0;
export function storageSummary(catalog, disk) {
 const categories=[{id:'lxl',name:'LXL',detail:'Pico 录制',bytes:0,count:0},{id:'dagger',name:'DAgger',detail:'人工示教 · 策略 HIL',bytes:0,count:0}];
 const seen=new Set();let missingSizes=false;
 for(const row of (catalog && catalog.episodes)||[]){
  const key=row.path||row.id;if(!key||seen.has(key))continue;seen.add(key);
  const bucket=['lxl','pico_capture','lerobot'].includes(row.source)?categories[0]:['hil_demonstration','hil_policy_hil'].includes(row.source)?categories[1]:null;
  if(!bucket)continue;
  bucket.count++;if(valid(row.size_bytes))bucket.bytes+=row.size_bytes;else missingSizes=true;
 }
 const total=categories.reduce((n,c)=>n+c.bytes,0),count=categories.reduce((n,c)=>n+c.count,0);
 const available=!!catalog&&Array.isArray(catalog.episodes),partial=!!(catalog&&(catalog.error||catalog.available===false))||missingSizes;
 const capacity=disk&&valid(disk.total_bytes)&&disk.total_bytes>0&&valid(disk.free_bytes)&&valid(disk.used_bytes)&&disk.used_bytes+disk.free_bytes<=disk.total_bytes+4096
  ?{total:disk.total_bytes,free:disk.free_bytes,used:disk.used_bytes,reserved:Math.max(0,disk.total_bytes-disk.used_bytes-disk.free_bytes)}:null;
 return {categories,total,count,available,partial,capacity};
}
const gb=n=>n===0?'0 GB':n<1e7?'<0.01 GB':`${(n/1e9).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})} GB`;
export function storageHTML(catalog,disk,{stale=false}={}){
 const s=storageSummary(catalog,disk),d=s.capacity;
 const colors={lxl:'#d7ad59',dagger:'#688bc0',other:'#959ba5',free:'#eeece7'};
 // One cylinder represents the whole disk; small recordings keep their real scale.
 const other=d?Math.max(0,d.total-d.free-s.total):0;
 const segments=d?[{id:'other',bytes:other},...s.categories,{id:'free',bytes:d.free}]:[];
 let y=250;
 const bands=segments.map(c=>{const h=220*c.bytes/d.total;y-=h;return `<path d="M34 ${y} Q100 ${y+32} 166 ${y} V${y+h} Q100 ${y+h+32} 34 ${y+h}Z" fill="${colors[c.id]}"/>`;}).join('');
 const graphic=`<svg class="storage-cylinder" viewBox="0 0 200 280" role="img" aria-label="${d?'磁盘存储：LXL、DAgger、系统及其他、剩余空间':'磁盘容量尚未读取'}"><defs><clipPath id="storage-body"><path d="M34 30 A66 16 0 0 1 166 30 V250 A66 16 0 0 1 34 250Z"/></clipPath><linearGradient id="storage-shade"><stop stop-color="#000" stop-opacity=".14"/><stop offset=".35" stop-color="#fff" stop-opacity=".12"/><stop offset="1" stop-color="#000" stop-opacity=".17"/></linearGradient></defs><g clip-path="url(#storage-body)"><rect x="34" y="14" width="132" height="252" fill="${d?colors.other:colors.free}"/>${bands}<rect x="34" y="14" width="132" height="252" fill="url(#storage-shade)"/></g><ellipse cx="100" cy="30" rx="66" ry="16" fill="${colors.free}" stroke="#d8d6d1" stroke-width=".6"/></svg>`;
 const rows=s.categories.map(c=>`<div class="storage-category"><span class="storage-label"><i style="background:${colors[c.id]}"></i>${c.name}</span><strong>${s.available?gb(c.bytes):'—'}<small>${s.available?c.count:'—'} 条</small></strong></div>`).join('');
 return `<div class="storage-heading"><h2>存储</h2><span>${stale?'上次读取':s.partial?'部分目录未读取':d?`共 ${gb(d.total)}`:''}</span></div><div class="storage-layout">${graphic}<div class="storage-legend">${rows}<div class="storage-other" title="含系统文件、导出数据、缓存和系统预留空间"><span class="storage-label"><i style="background:${colors.other}"></i>系统及其他</span><span>${d?gb(other):'—'}</span></div><div class="storage-remaining"><span>剩余空间</span><strong class="storage-free">${d?gb(d.free):'—'}</strong></div></div></div>`;
}

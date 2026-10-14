// Browse recording sources, independently of historical on-disk roots.
// Exported training segments never count as newly recorded takes.
export function libraryView(catalog, current='') {
 const seen=new Set(),records=[],exports=[];
 for(const e of (catalog&&catalog.episodes)||[]){
  const key=e.path||e.id;if(!key||seen.has(key))continue;seen.add(key);
  if(e.source==='unified_dataset'){exports.push(e);continue;}
  let parts;
  if(e.source==='hil_demonstration')parts=['DAgger','人工示教',e.name];
  else if(e.source==='hil_policy_hil')parts=['DAgger','策略 HIL',e.name];
  else if(e.source==='lxl')parts=['LXL',...(e.relative_path||e.name).split('/').filter(Boolean)];
  else if(['pico_capture','lerobot'].includes(e.source))parts=['LXL','独立上肢录制',e.name];
  else parts=['其他录制',e.name];
  records.push({...e,parts});
 }
 const allowed=new Set(['','DAgger','DAgger/人工示教','DAgger/策略 HIL']);
 for(const e of records)for(let i=1;i<e.parts.length;i++)allowed.add(e.parts.slice(0,i).join('/'));
 if(!allowed.has(current))current='';
 const prefix=current?current.split('/'):[],branches=new Map(),leaves=[];
 if(current==='DAgger'){branches.set('人工示教',[]);branches.set('策略 HIL',[]);}
 for(const e of records){
  if(!prefix.every((p,i)=>e.parts[i]===p))continue;
  const rest=e.parts.slice(prefix.length);
  if(rest.length===1)leaves.push(e);
  else if(rest.length>1){if(!branches.has(rest[0]))branches.set(rest[0],[]);branches.get(rest[0]).push(e);}
 }
 const folders=[...branches].map(([name,rows])=>({name,path:[...prefix,name].join('/'),count:rows.length,bytes:rows.reduce((n,e)=>n+(e.size_bytes||0),0)}));
 leaves.sort((a,b)=>String(b.started_at||b.modified_at||'').localeCompare(String(a.started_at||a.modified_at||'')));
 return {records,exports,current,prefix,folders,leaves};
}

// A bounded page of the current source, including nested legacy directories.
// Deletion eligibility remains with libraryView().leaves, never this subtree.
export function libraryPage(view,{query='',review='all',kind='recordings',page=1,pageSize=12}={}) {
 const terms=String(query).trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
 const candidates=kind==='exports'?view.exports:view.records.filter(e=>view.prefix.every((p,i)=>e.parts[i]===p));
 const filtered=candidates.filter(e=>{
  const text=[e.name,e.relative_path,e.source,...(e.parts||[])].filter(Boolean).join(' ').toLocaleLowerCase();
  return terms.every(term=>text.includes(term))&&(review==='all'||((e.review&&e.review.status)||'pending')===review);
 }).sort((a,b)=>String(b.started_at||b.modified_at||'').localeCompare(String(a.started_at||a.modified_at||''))||String(a.id).localeCompare(String(b.id)));
 const size=Math.max(1,Math.min(100,Math.floor(Number(pageSize))||12)),pages=Math.max(1,Math.ceil(filtered.length/size));
 const current=Math.max(1,Math.min(pages,Math.floor(Number(page))||1)),start=(current-1)*size;
 return {rows:filtered.slice(start,start+size),total:filtered.length,page:current,pages,pageSize:size,start:filtered.length?start+1:0,end:Math.min(start+size,filtered.length)};
}

export function canonicalFolder(value=''){for(const [old,next] of [['DAgger/人工示教','HIL/人工示教'],['DAgger/策略 HIL','HIL/DAgger'],['真机rollout','真机Rollout'],['DAgger','HIL']])if(value===old||value.startsWith(old+'/'))return next+value.slice(old.length);return value;}
export function recordTime(value){if(value===null||value===undefined||value==='')return NaN;const n=typeof value==='number'?value:Number(value);return Number.isFinite(n)?(n<1e12?n*1000:n):Date.parse(value);}
// Browse recording sources, independently of historical on-disk roots.
// Exported training segments never count as newly recorded takes.
export function libraryView(catalog, current='') {
 current=canonicalFolder(current);
 const seen=new Set(),records=[],exports=[];
 for(const e of (catalog&&catalog.episodes)||[]){
  const key=e.path||e.id;if(!key||seen.has(key))continue;seen.add(key);
  if(e.source==='unified_dataset'){exports.push(e);continue;}
  let parts;
  if(e.library_folder)parts=[...canonicalFolder(e.library_folder).split('/'),e.name];
  else if(e.source==='hil_demonstration')parts=['HIL','人工示教',e.name];
  else if(e.source==='hil_policy_hil')parts=['HIL','DAgger',e.name];
  else if(e.source==='lxl')parts=['LXL',...(e.relative_path||e.name).split('/').filter(Boolean)];
  else if(['pico_capture','lerobot'].includes(e.source))parts=['LXL','独立上肢录制',e.name];
  else parts=['其他录制',e.name];
  records.push({...e,parts});
 }
 const nodes=new Map();
 const addSource=path=>{const parts=path.split('/').filter(Boolean);for(let i=0;i<parts.length;i++){const key=parts.slice(0,i+1).join('/');if(!nodes.has(key))nodes.set(key,{path:key,parent:parts.slice(0,i).join('/'),name:parts[i],custom:false});}};
 addSource('LXL');addSource('真机Rollout');addSource('HIL/人工示教');addSource('HIL/DAgger');addSource('HIL/RLT');
 for(const e of records){e.sourceFolder=e.parts.slice(0,-1).join('/');addSource(e.sourceFolder);}
 const original=(catalog&&catalog.organization)||{},organization={...original,folders:(original.folders||[]).map(f=>({...f,parent:canonicalFolder(f.parent)}))},memberships=Object.fromEntries(Object.entries(original.memberships||{}).map(([id,m])=>[id,{...m,folder:canonicalFolder(m.folder)}]));
 for(const f of organization.folders||[]){if(!f.parent.startsWith('@'))addSource(f.parent);nodes.set(f.id,{path:f.id,parent:f.parent,name:f.name,custom:true});}
 for(const m of Object.values(memberships))if(!m.folder.startsWith('@'))addSource(m.folder);
 const ancestry=path=>{const chain=[],seen=new Set();while(path&&nodes.has(path)&&!seen.has(path)){seen.add(path);const n=nodes.get(path);chain.unshift(n);path=n.parent;}return chain;};
 for(const e of records){const m=memberships[e.id];e.folder=m&&m.source_signature===e.source_signature&&(!m.folder||nodes.has(m.folder))?m.folder:e.sourceFolder;e.ancestry=ancestry(e.folder).map(n=>n.path);e.parts=[...ancestry(e.folder).map(n=>n.name),e.name];}
 if(current&&!nodes.has(current))current='';
 const breadcrumbs=ancestry(current),prefix=breadcrumbs.map(n=>n.name);
 const allFolders=[...nodes.values()].map(n=>{const rows=records.filter(e=>e.ancestry.includes(n.path));return {...n,count:rows.length,bytes:rows.reduce((sum,e)=>sum+(e.size_bytes||0),0),ancestors:ancestry(n.path).map(n=>n.path)};});
 const folders=allFolders.filter(n=>n.parent===current),leaves=records.filter(e=>e.folder===current).sort(recordOrder);
 const scoped=records.filter(e=>!current||e.ancestry.includes(current)).sort(recordOrder);
 // Archives describe remote originals and never enter local recording counts,
 // drag/selection, Replay, or the storage byte totals.
 const archives=((catalog&&catalog.archives)||[]).filter(a=>{
  const folder=canonicalFolder(a.library_folder||'');
  return !current||folder===current||folder.startsWith(current+'/');
 });
 const archivesError=(!current||current==='HIL'||current==='HIL/DAgger')&&catalog&&catalog.archives_error||'';
 return {records,exports,current,prefix,breadcrumbs,allFolders,folders,leaves,scoped,archives,archivesError};
}

export function recordOrder(a,b){
 const at=recordTime(a.started_at||a.modified_at),bt=recordTime(b.started_at||b.modified_at);
 return (Number.isFinite(bt)?bt:-Infinity)-(Number.isFinite(at)?at:-Infinity)||String(a.id).localeCompare(String(b.id));
}
export function captureMode(episode){
 return episode.capture_mode||({hil:'dagger'}[episode.recording_kind]||episode.recording_kind)||({lxl:'lxl',pico_capture:'lxl',lerobot:'lxl',hil_demonstration:'demonstration',hil_policy_hil:'dagger'}[episode.source])||'unknown';
}
export function folderPage(view,{query='',page=1,pageSize=8,scope='folder',reviewStatus='',dateFrom='',dateTo='',browseMode='folders',assessmentFilter='',sourceMode=''}={}) {
 const terms=String(query).trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
 const timeline=browseMode==='time';
 const filtered=timeline||terms.length||reviewStatus||dateFrom||dateTo||scope==='all'||browseMode==='all'||assessmentFilter||sourceMode;
 const from=dateFrom?new Date(dateFrom+'T00:00:00').getTime():-Infinity,to=dateTo?new Date(dateTo+'T23:59:59.999').getTime():Infinity;
 const matched=(filtered?view.records:view.leaves).filter(e=>{
  if(!timeline&&scope!=='all'&&view.current&&!e.ancestry.includes(view.current))return false;
  if(sourceMode&&captureMode(e)!==sourceMode)return false;
  if(!terms.every(term=>[e.name,e.capture_label,e.relative_path,e.review&&!e.review.stale?e.review.notes:'',...e.parts].join(' ').toLocaleLowerCase().includes(term)))return false;
  if(reviewStatus&&(e.review&&e.review.stale?'pending':(e.review&&e.review.status)||'pending')!==reviewStatus)return false;
  if(assessmentFilter){const a=rltAssessment(e);if(!a||assessmentFilter==='pending'&&a.split!=='pending'||assessmentFilter==='manual_confirmation'&&a.confirmation_kind!=='manual_confirmation')return false;}
  const time=recordTime(e.started_at||e.modified_at);return !dateFrom&&!dateTo||Number.isFinite(time)&&time>=from&&time<=to;
 }).sort(recordOrder);
 const entries=[...(!filtered?view.folders.map(folder=>({folder})):[]),...matched.map(episode=>({episode}))];
 const size=Math.max(1,Math.min(100,Math.floor(Number(pageSize))||8)),pages=Math.max(1,Math.ceil(entries.length/size));
 const current=Math.max(1,Math.min(pages,Math.floor(Number(page))||1)),offset=(current-1)*size,rows=entries.slice(offset,offset+size);
 return {matching:matched,folders:rows.filter(row=>row.folder).map(row=>row.folder),leaves:rows.filter(row=>row.episode).map(row=>row.episode),total:entries.length,page:current,pages,start:entries.length?offset+1:0,end:Math.min(offset+size,entries.length)};
}

// This is presentation of the backend's source-bound assessment, never an
// inference from editable notes, filename, a saved take, or a green video badge.
export function rltAssessment(episode){
 if(!episode||episode.capture_mode!=='rlt')return null;
 const value=episode.rlt_assessment;
 if(!value||!episode.source_signature||value.source_signature!==episode.source_signature||value.stale||value.quality==='stale')return {split:'pending',outcome_label:'结果待核实',confirmation_label:'待审核',usage_label:'待审核',notes_summary:value?'审核来源已变化，需重新核对。':'尚无局部数据审核结论。',source_signature:episode.source_signature};
 const split=value.split==='exclude'?'excluded':value.split;
 return {...value,split:['train','validation','excluded','pending'].includes(split)?split:'pending'};
}

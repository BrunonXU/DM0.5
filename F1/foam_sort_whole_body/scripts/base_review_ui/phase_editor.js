'use strict';

// This editor records evidence. It does not approve datasets or run the robot.
window.createBasePhaseEditor = function ({container, seek, currentTime, frameReady}) {
  let source=null, rows=[], notes='', generation=0, recoveryRaw=null;
  const memoryDrafts=new Map();
  const defaults=[['去程后退',-.5,0],['去程右移',0,-1.6],['去程前进',.5,0],
                  ['返程后退',-.5,0],['返程左移',0,1.6],['返程前进',.5,0]];
  const el=(tag,attrs={},value='')=>{
    const e=document.createElement(tag);Object.entries(attrs).forEach(([k,v])=>e.setAttribute(k,v));e.textContent=value;return e;
  };
  const key=()=>`f1-base-phase-review-v1:${source.episode}:${source.source_origin_ns}`;
  const message=value=>container.querySelector('.phase-editor-status').textContent=value;
  const fresh=(name,dx=0,dy=0,kind='motion')=>({id:`segment_${Date.now()}_${Math.random().toString(36).slice(2,8)}`,
    name,cycle_id:'cycle_1',kind,start_s:null,end_s:null,dx_m:dx,dy_m:dy,dyaw_rad:0,reviewed:false,evidence_note:''});
  const draftObject=()=>({schema:'f1_base_phase_editor_draft_v1',...source,rows,notes});
  function validDraft(saved) {
    if(saved?.schema!=='f1_base_phase_editor_draft_v1'||saved.episode!==source.episode||saved.source_origin_ns!==source.source_origin_ns||!Array.isArray(saved.rows))return false;
    const ids=new Set();
    return saved.rows.every(r=>{
      if(!r||!['motion','stationary'].includes(r.kind)||typeof r.reviewed!=='boolean')return false;
      if(!['id','name','cycle_id','evidence_note'].every(k=>typeof r[k]==='string')||!r.id||ids.has(r.id))return false;
      ids.add(r.id);
      return ['start_s','end_s','dx_m','dy_m','dyaw_rad'].every(k=>r[k]===null||Number.isFinite(r[k]));
    });
  }
  function save() {
    if(!source)return;
    const priorExport=container.querySelector('.phase-json-output');if(priorExport)priorExport.value='';
    const raw=JSON.stringify(draftObject());memoryDrafts.set(key(),raw);
    try {localStorage.setItem(key(),raw);message('逐段草稿已保存在当前浏览器。');}
    catch(_){message('浏览器无法持久保存。当前页切换仍保留，请点“备份草稿”保存未完成内容。');}
    showRowProblems();
  }
  function rowProblems() {
    const problems=new Map(rows.map(r=>[r.id,[]]));
    const add=(r,note)=>problems.get(r.id).push(note);
    for(const r of rows){
      if(r.start_s===null&&r.end_s===null)continue;
      if(!Number.isFinite(r.start_s)||!Number.isFinite(r.end_s))add(r,'开始和结束尚未填完整；可备份草稿。');
      else if(r.start_s<0||r.start_s>=r.end_s||r.end_s>source.source_duration_s)add(r,'起止倒序或超出本条录制范围。');
    }
    const spans=rows.filter(r=>Number.isFinite(r.start_s)&&Number.isFinite(r.end_s)&&r.start_s<r.end_s);
    for(let i=0;i<spans.length;i++)for(let j=i+1;j<spans.length;j++){
      const a=spans[i],b=spans[j];
      if(Math.max(a.start_s,b.start_s)<Math.min(a.end_s,b.end_s)){
        add(a,`与“${b.name}”时段重叠，请核对秒数。`);add(b,`与“${a.name}”时段重叠，请核对秒数。`);
      }
    }
    return problems;
  }
  function showRowProblems() {
    if(!source)return;
    const problems=rowProblems();
    for(const card of container.querySelectorAll('.phase-row')){
      const messages=problems.get(card.dataset.rowId)||[];
      card.classList.toggle('phase-row-invalid',messages.length>0);
      const error=card.querySelector('.phase-row-error');error.textContent=messages.join(' ');error.hidden=!messages.length;
      for(const field of card.querySelectorAll('input[data-phase-field="start_s"],input[data-phase-field="end_s"]'))field.setAttribute('aria-invalid',String(messages.length>0));
    }
  }
  function invalidate(row) {row.reviewed=false;}
  function input(row,field,label,type='number') {
    const wrap=el('label',{'class':'phase-field'},label);
    const i=el('input',{type,'aria-label':`${row.name} ${label}`,'data-phase-field':field});i.value=row[field]??'';
    if(type==='number')i.step='0.001';
    i.addEventListener('input',()=>{
      row[field]=type==='number'?(i.value===''?null:Number(i.value)):i.value;
      invalidate(row);const box=wrap.closest('.phase-row')?.querySelector('.phase-reviewed input');if(box)box.checked=false;save();
    });wrap.append(i);return wrap;
  }
  function timeInput(row,field,label) {
    const wrap=input(row,field,label),b=el('button',{type:'button'},'此刻');
    b.addEventListener('click',()=>{
      if(!frameReady()){message('先播放或定位到实际视频帧，再记录此刻。');return;}
      row[field]=Math.min(Math.floor(source.source_duration_s*1000)/1000,Math.round(currentTime()*1000)/1000);invalidate(row);save();renderRows();
    });wrap.append(b);return wrap;
  }
  function renderRows() {
    const list=container.querySelector('.phase-rows');list.replaceChildren();
    rows.forEach((row,index)=>{
      const card=el('section',{'class':'phase-row','data-row-id':row.id}),heading=el('div',{'class':'phase-row-heading'});
      heading.append(el('strong',{},`${index+1}. ${row.name}`));
      const jump=el('button',{type:'button'},'查看起点');
      jump.addEventListener('click',()=>{if(Number.isFinite(row.start_s))seek(Math.max(0,row.start_s-.5));else message('请先填写开始时间。');});heading.append(jump);
      const remove=el('button',{type:'button','aria-label':`移除${row.name}`},'移除空段');
      remove.addEventListener('click',()=>{if(row.start_s!==null||row.end_s!==null){message('先清空此段起止，再移除，避免误删已标时段。');return;}rows.splice(index,1);save();renderRows();});
      heading.append(remove);card.append(heading);
      const fields=el('div',{'class':'phase-row-fields'});
      fields.append(timeInput(row,'start_s','开始秒'),timeInput(row,'end_s','结束秒'),input(row,'cycle_id','循环','text'));
      if(row.kind==='motion')fields.append(input(row,'dx_m','前向位移 m'),input(row,'dy_m','左向位移 m'),input(row,'dyaw_rad','转角 rad'));
      else fields.append(el('span',{'class':'subtle'},'显式静止段：三维速度为零；未填写的空隙仍为未知。'));
      card.append(fields,el('p',{'class':'phase-row-error','role':'status',hidden:''}));
      const evidence=input(row,'evidence_note','画面依据（例如：柜体开始右移，头部未转动）','text');card.append(evidence);
      const checked=el('label',{'class':'phase-reviewed'}),box=el('input',{type:'checkbox','aria-label':`${row.name} 时段方向已核对`});box.checked=row.reviewed;
      box.addEventListener('change',()=>{
        if(box.checked && (rowProblems().get(row.id).length||!Number.isFinite(row.start_s)||!Number.isFinite(row.end_s)||row.start_s>=row.end_s||!row.evidence_note.trim())){
          box.checked=false;message('先填写合法起止和画面依据，再确认该段。');return;
        }
        row.reviewed=box.checked;save();
      });checked.append(box,document.createTextNode('已看画面核对本段时序、方向（距离仍为先验）'));card.append(checked);list.append(card);
    });
    showRowProblems();
  }
  function exportReview() {
    if(!validDraft(draftObject()))throw new Error('草稿字段格式不合法，请先修复或备份草稿。');
    const segments=[];
    for(const row of rows) {
      if(row.start_s===null && row.end_s===null)continue;
      if(!Number.isFinite(row.start_s)||!Number.isFinite(row.end_s)||row.start_s<0||row.start_s>=row.end_s||row.end_s>source.source_duration_s)
        throw new Error(`${row.name}：起止不完整、倒序或超出录制。`);
      if(!row.cycle_id.trim())throw new Error(`${row.name}：请填写循环名称。`);
      for(const k of ['dx_m','dy_m','dyaw_rad'])if(!Number.isFinite(row[k]))throw new Error(`${row.name}：${k} 不是有效数值。`);
      const zero=['dx_m','dy_m','dyaw_rad'].every(k=>row[k]===0);
      if(row.kind==='motion'&&zero)throw new Error(`${row.name}：移动段不能全零；真正静止请单独添加静止段。`);
      if(row.kind==='stationary'&&!zero)throw new Error(`${row.name}：静止段三个位移必须为零。`);
      if(row.reviewed&&!row.evidence_note.trim())throw new Error(`${row.name}：已核对的段必须有画面依据。`);
      if(row.kind==='stationary' && !row.evidence_note.trim())throw new Error('静止段必须填写画面依据；低像素位移不等于底盘静止。');
      const evidence=row.evidence_note.trim()?[{kind:'visual_review_note',note:row.evidence_note.trim(),time_axis:'seconds_since_original_mcap_log_start'}]:[];
      if(row.proposal_evidence)evidence.push({kind:'original_assistant_proposal',phase_sha256:row.proposal_sha256,evidence:row.proposal_evidence});
      segments.push({id:row.id,name:row.name,cycle_id:row.cycle_id,kind:row.kind,start_s:row.start_s,end_s:row.end_s,
        dx_m:row.dx_m,dy_m:row.dy_m,dyaw_rad:row.dyaw_rad,timing_status:row.reviewed?'reviewed':'proposed',
        reviewer:row.reviewed?'browser_user':'',distance_provenance:row.kind==='stationary'?'explicit_visual_stationary_review':'task_route_prior_not_measured',evidence});
    }
    segments.sort((a,b)=>a.start_s-b.start_s);
    for(let i=1;i<segments.length;i++)if(segments[i].start_s<segments[i-1].end_s)throw new Error('移动段有重叠；同时沿两轴移动请用一个段填写 dx/dy。');
    return {schema:'f1_base_phase_review_v1',...source,coordinate_convention:'+x forward, +y left',
      segments,notes,reviewer:'browser_user',created_at:new Date().toISOString(),training_approved:false,
      missing_rows:rows.filter(r=>r.start_s===null&&r.end_s===null).map(r=>r.name),
      limitations:'仅逐段重标输入；未覆盖时间为未知，距离为先验，尚未通过全19D数据验收。'};
  }
  function exportFile() {
    container.querySelector('.phase-json-output').value='';
    try {
      const review=exportReview();save();
      const blob=new Blob([JSON.stringify(review,null,2)+'\n'],{type:'application/json'}),url=URL.createObjectURL(blob);
      const a=el('a',{href:url,download:`${source.episode.replaceAll('/','_')}_base_phase_review.json`});document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
      container.querySelector('.phase-json-output').value=JSON.stringify(review,null,2);
      message(`已导出 ${review.segments.length} 段；未填部分仍为未知。将 JSON 提供给6000D重标工具即可继续。`);
    } catch(error){message(error.message);}
  }
  function backup(raw,name) {
    const url=URL.createObjectURL(new Blob([raw+'\n'],{type:'application/json'}));
    const a=el('a',{href:url,download:name});document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
  }
  async function suggestions(token) {
    const region=container.querySelector('.phase-suggestions');
    try {
      const id=source.episode.replaceAll('/','_');
      const response=await fetch(`phase_suggestions/${id}.json`,{cache:'no-cache'});
      if(!response.ok)throw new Error('尚无本条候选');const data=await response.json();
      if(token!==generation)return;
      region.replaceChildren(el('p',{'class':'subtle'},'以下仅为背景运动/变化线索，点击查看；不会自动填成机器人方向或真实启停。'));
      const intervals=(data.intervals||[]).filter(r=>r.kind==='motion'||r.kind==='image_motion'||r.kind==='trusted_motion');
      const turns=data.transitions||[];
      [...intervals.map(r=>({...r,label:'背景运动'})),...turns.map(r=>({...r,label:'变化候选'}))].sort((a,b)=>a.start_s-b.start_s).forEach(r=>{
        const b=el('button',{type:'button'},`${r.label} ${r.start_s.toFixed(2)}–${r.end_s.toFixed(2)}秒`);
        b.addEventListener('click',()=>seek(Math.max(0,r.start_s-1)));region.append(b);
      });
      if(!intervals.length&&!turns.length)region.append(el('p',{'class':'subtle'},'暂无可靠候选；请直接查看完整视频。'));
    } catch(_){if(token===generation)region.textContent='候选证据尚未载入；可直接按完整视频记录。';}
  }
  function setProposal(phase,phaseSha) {
    if(!source||phase.episode!==source.episode||phase.source_origin_ns!==source.source_origin_ns)return;
    const actions=container.querySelector('.phase-proposal');if(!actions)return;
    actions.replaceChildren(el('p',{'class':'subtle'},`本条已有逐帧复看的建议：${phase.segments.filter(s=>s.kind==='motion').length}段移动、${phase.segments.filter(s=>s.kind==='stationary').length}段停顿。不要求凑齐六段；所有建议尚待确认，距离和速度曲线仍为先验。`));
    const button=el('button',{type:'button','id':'load-phase-proposal'},'载入建议阶段（待确认）');
    button.addEventListener('click',()=>{
      if(rows.some(r=>r.start_s!==null||r.end_s!==null||r.evidence_note)){
        message('本条已有逐段草稿；保留现有内容，请对照建议曲线自行调整。原任务锚点不受影响。');return;
      }
      const names={initial_pick_pause:'取料停顿',outbound_backward:'去程后退',lift_pause:'抬臂停顿',outbound_right:'去程右移',outbound_forward:'去程前进',release_pause:'放料停顿',return_backward:'返程后退',return_turn_pause:'返程转向停顿',return_left:'返程左移',reset_pause:'返回后停顿 / 双臂复位'};
      rows=phase.segments.map(s=>({...fresh(s.name||names[s.id]||s.id,s.dx_m,s.dy_m,s.kind),id:s.id,cycle_id:String(s.cycle_id),
        start_s:s.start_s,end_s:s.end_s,dyaw_rad:s.dyaw_rad,reviewed:false,
        proposal_evidence:s.evidence,proposal_sha256:phaseSha,
        evidence_note:'待核对：'+(s.evidence?.observation||JSON.stringify(s.evidence))}));
      source.provenance={...source.provenance,assistant_proposal_sha256:phaseSha};
      save();renderRows();message('已载入建议值，全部保持未核对；请看画面修正后再勾选。上方虚线展示发布时的建议，不随草稿编辑自动更新。');
    });actions.append(button);
  }
  function setSource(value) {
    generation++;source=value;
    rows=defaults.map(([name,dx,dy])=>fresh(name,dx,dy));notes='';recoveryRaw=null;
    try {
      const raw=memoryDrafts.get(key())||localStorage.getItem(key());recoveryRaw=raw;
      const saved=JSON.parse(raw);
      if(validDraft(saved)){rows=saved.rows;notes=typeof saved.notes==='string'?saved.notes:'';recoveryRaw=null;}
    }catch(_){}
    container.replaceChildren();
    const details=el('details',{'class':'phase-editor-details'});details.open=true;
    details.append(el('summary',{},'逐段修复底盘标签'));
    details.append(el('p',{'class':'subtle'},'每段填实际起止，段间停顿单独添加。坐标按机器人：前方 +x、左方 +y。0.5/1.6米已获现场路线确认，逐时刻速度仍是重建估计。空段和空隙保持未知；红框表示填写冲突，草稿仍会保留。'));
    details.append(el('div',{'class':'phase-proposal'}),el('div',{'class':'phase-suggestions subtle'},'正在读取候选证据…'),el('div',{'class':'phase-rows'}));
    const actions=el('div',{'class':'phase-editor-actions'});
    for(const [kind,label] of [['motion','添加移动段'],['stationary','添加静止段']]){
      const b=el('button',{type:'button'},label);b.addEventListener('click',()=>{rows.push(fresh(label,0,0,kind));save();renderRows();});actions.append(b);
    }
    const note=el('textarea',{rows:'2','aria-label':'逐段修复备注',placeholder:'看不清的区间、距离依据或多轮任务说明'});note.value=notes;note.addEventListener('input',()=>{notes=note.value;save();});
    const download=el('button',{type:'button','class':'primary','id':'download-phase-review'},'下载逐段 JSON');download.addEventListener('click',exportFile);actions.append(download);
    const backupButton=el('button',{type:'button','id':'backup-phase-draft'},'备份草稿');backupButton.addEventListener('click',()=>backup(JSON.stringify(draftObject(),null,2),`${source.episode.replaceAll('/','_')}_phase_draft.json`));actions.append(backupButton);
    if(recoveryRaw){const recover=el('button',{type:'button'},'下载无法恢复的旧草稿');recover.addEventListener('click',()=>backup(recoveryRaw,'phase_draft_recovery.json'));actions.append(recover);}
    details.append(actions,note,el('p',{'class':'phase-editor-status subtle','role':'status'},'与上方任务锚点分别保存；不会修改已有草稿或原标签。'));
    const raw=el('details');raw.append(el('summary',{},'查看导出内容'),el('textarea',{'class':'phase-json-output',readonly:'',rows:'6'}));details.append(raw);container.append(details);
    renderRows();suggestions(generation);
  }
  return {setSource,setProposal,clear(){generation++;source=null;rows=[];container.replaceChildren();},exportReview};
};

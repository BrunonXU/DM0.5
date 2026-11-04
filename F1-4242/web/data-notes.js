// The editor lives outside the polled catalog so refreshes cannot replace input.
export class DataNotesEditor {
 constructor({api,esc,formatDate,drafts,persist,getEpisode,onSaved}){Object.assign(this,{api,esc,formatDate,drafts,persist,getEpisode,onSaved});this.dialog=null;}
 open(episode){
  if(this.dialog?.open)return;
  // close events are queued; an immediate reopen must not leave duplicate IDs.
  if(this.dialog){this.dialog.remove();this.dialog=null;}
  const {esc}=this,id=episode.id,draft=this.drafts.get(id);
  const dialog=document.createElement('dialog');dialog.className='data-note-dialog';
  dialog.setAttribute('aria-labelledby','data-note-title');
  dialog.innerHTML=`<form><div class="data-note-heading"><h2 id="data-note-title">编辑备注</h2><button type="button" class="data-note-close" data-note-close aria-label="关闭">×</button></div><p class="data-note-meta">${esc(this.formatDate(episode.started_at||episode.modified_at,episode.display_timezone))}</p><textarea data-note-input aria-label="备注内容" maxlength="4000" placeholder="布局变化、值得留意的片段…">${esc(draft?.notes??episode.review?.notes??'')}</textarea><div class="data-note-meta-row"><span>Ctrl + Enter 保存</span><span data-note-count></span></div><div data-note-error class="notice warn" role="status" hidden></div><div data-note-conflict hidden><p>最新备注</p><pre data-note-latest></pre><div class="data-note-conflict-actions"><button type="button" class="button small" data-note-reload>使用最新内容</button><button type="button" class="button small" data-note-keep>保留草稿继续编辑</button></div></div><div class="data-note-actions"><button type="button" class="button" data-note-close>取消</button><button class="button primary" data-note-save type="submit">保存备注</button></div></form>`;
  const state={episode:structuredClone(episode),source:draft?.source_signature||episode.source_signature,revision:draft?.review_revision??episode.review?.revision,saving:false,latest:null,dialog};
  this.dialog=dialog;this.state=state;document.body.append(dialog);
  const q=s=>dialog.querySelector(s),input=q('[data-note-input]');
  const showError=message=>{q('[data-note-error]').textContent=message;q('[data-note-error]').hidden=!message;};
  const count=()=>q('[data-note-count]').textContent=`${input.value.length} / 4000`;
  const cache=()=>{const old=this.drafts.get(id);this.drafts.set(id,{status:old?.status||episode.review?.status||'pending',notes:input.value,source_signature:state.source,review_revision:state.revision,version:(old?.version||0)+1});this.persist();count();};
  input.addEventListener('input',cache);input.addEventListener('keydown',event=>{if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();q('form').requestSubmit();}});
  dialog.querySelectorAll('[data-note-close]').forEach(button=>button.onclick=()=>dialog.close());
  dialog.addEventListener('close',()=>{dialog.remove();if(this.dialog===dialog){this.dialog=null;this.state=null;document.querySelector(`[data-note-edit="${id}"]`)?.focus({preventScroll:true});}});
  q('[data-note-reload]').onclick=()=>{const latest=state.latest;if(!latest)return;state.episode=latest;state.source=latest.source_signature;state.revision=latest.review?.revision;input.value=latest.review?.notes||'';this.drafts.delete(id);this.persist();count();q('[data-note-conflict]').hidden=true;showError('');q('[data-note-save]').disabled=false;input.focus();};
  q('[data-note-keep]').onclick=()=>{const latest=state.latest;if(!latest||latest.source_signature!==state.source)return;state.revision=latest.review?.revision;cache();q('[data-note-conflict]').hidden=true;showError('');q('[data-note-save]').disabled=false;input.focus();};
  q('form').addEventListener('submit',async event=>{
   event.preventDefault();if(state.saving)return;cache();
   const snapshot={...this.drafts.get(id)},catalogSource=this.getEpisode(id)?.source_signature;state.saving=true;q('[data-note-save]').disabled=true;q('[data-note-save]').textContent='保存中…';showError('');
   try{
    const result=await this.api('/api/data/notes',{episode_id:id,notes:snapshot.notes,expected_source_signature:snapshot.source_signature,expected_review_revision:snapshot.review_revision});
    const known=this.getEpisode(id),sourceReplaced=catalogSource===snapshot.source_signature&&known&&known.source_signature!==snapshot.source_signature;
    const pending=this.drafts.get(id),newer=sourceReplaced||pending?.version!==snapshot.version||pending?.source_signature!==snapshot.source_signature;
    if(!newer)this.drafts.delete(id);else if(!sourceReplaced&&pending?.source_signature===snapshot.source_signature)pending.review_revision=result.review.revision;
    this.persist();state.revision=result.review.revision;this.onSaved(id,snapshot.source_signature,result.review);
    if(!newer){if(dialog.open)dialog.close();}else if(dialog.open)showError(sourceReplaced?'来源已更新，请重新读取；旧草稿已保留。':'已保存；继续编辑的内容尚未保存。');
   }catch(error){
    showError(error.message);
    if(error.status===409){
     try{const result=await this.api('/api/data/episodes/'+encodeURIComponent(id));state.latest=result.episode;q('[data-note-latest]').textContent=result.episode.review?.notes||'暂无备注';q('[data-note-conflict]').hidden=false;q('[data-note-keep]').hidden=state.source!==result.episode.source_signature;}catch{showError(error.message+'；最新内容读取失败，请稍后重试。');}
    }
   }finally{state.saving=false;q('[data-note-save]').textContent='保存备注';q('[data-note-save]').disabled=!!state.latest&&!q('[data-note-conflict]').hidden;}
  });
  count();dialog.showModal();input.focus();
  if(state.source!==episode.source_signature){state.latest=episode;q('[data-note-latest]').textContent=episode.review?.notes||'暂无备注';q('[data-note-conflict]').hidden=false;q('[data-note-keep]').hidden=true;q('[data-note-save]').disabled=true;showError('来源已变化，旧草稿保留，请先读取当前内容。');}
 }
}

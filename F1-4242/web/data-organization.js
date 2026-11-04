import {libraryView} from './data-library.js';

// Logical organization only; the server owns persistence and source validation.
export class DataOrganizationUI {
 constructor({get,api,login,render,refresh,selection,esc}){
  Object.assign(this,{get,api,login,render,refresh,selection,esc});
  this.busy=false;this.dialog=null;this.drag=null;this.epoch=0;this.message='';this.collapsed=new Set();this.seenFolders=new Set();
  document.addEventListener('click',event=>this.click(event),true);
  document.addEventListener('dragstart',event=>this.startDrag(event));
  document.addEventListener('dragover',event=>this.over(event));
  document.addEventListener('drop',event=>this.drop(event));
  document.addEventListener('dragend',()=>this.endDrag());
 }
 get engaged(){return this.busy||!!this.dialog||!!this.drag;}
 get ready(){const s=this.get();return s.page==='data'&&!!s.data?.organization?.revision&&!s.dataReadFailed&&!s.deleting;}
 view(){return libraryView(this.get().data,this.get().folder||'');}
 actions(){const v=this.view(),disabled=!this.ready||this.engaged?'disabled':'';return `<div class="data-list-actions"><button class="button small" data-organize="create" ${disabled}>新建文件夹</button>${v.current.startsWith('@')?`<button class="button small" data-organize="rename" ${disabled}>重命名</button><button class="button small" data-organize="move-folder" ${disabled}>移至</button>`:''}${this.selection().length?`<button class="button small" data-organize="move-selected" ${disabled}>移至…</button>`:''}</div>`;}
 feedback(){const text=this.get().data?.organization_error||this.message;return text?`<div class="notice data-organization-message" role="status">${this.esc(text)}${this.failed?'<button class="button small" data-organize="refresh">重新读取</button>':''}</div>`:'';}
 renderTree(){
  const holder=document.querySelector('#data-version-tree');if(!holder)return;
  const v=this.view(),e=this.esc;
  for(const f of v.allFolders){if(!this.seenFolders.has(f.path)){this.seenFolders.add(f.path);this.collapsed.add(f.path);}}
  const children=parent=>v.allFolders.filter(f=>f.parent===parent);
  const branch=(parent,depth=0)=>children(parent).map(f=>{const has=children(f.path).length,closed=this.collapsed.has(f.path);return `<div class="data-tree-row" style="--level:${depth}">${has?`<button class="tree-toggle" data-tree-toggle="${e(f.path)}" aria-label="${closed?'展开':'收起'} ${e(f.name)}" aria-expanded="${!closed}">${closed?'▸':'▾'}</button>`:'<span class="tree-toggle"></span>'}<button class="${f.path===v.current&&this.get().browseMode!=='time'?'current':''}" data-folder="${e(f.path)}" ${f.custom?'draggable="true" data-drag-folder="'+e(f.path)+'"':''} title="${e(f.name)}"><span class="tree-folder" aria-hidden="true"></span><span class="tree-name">${e(f.name)}</span><small>${f.count}</small></button></div>${has&&!closed?branch(f.path,depth+1):''}`;}).join('');
  const html=`<h2>数据目录</h2><div class="data-version-tree"><div class="data-tree-row"><span class="tree-toggle"></span><button data-folder="" class="${!v.current&&this.get().browseMode!=='time'?'current':''}"><span class="tree-folder"></span><span class="tree-name">全部文件夹</span></button></div>${branch('')}</div>`;
  if(holder.innerHTML!==html)holder.innerHTML=html;
 }
 async click(event){
  if(this.suppressClick&&performance.now()<this.suppressClick&&event.target.closest('[data-episode],[data-folder],[data-select-episode]')){event.preventDefault();event.stopImmediatePropagation();return;}
  const toggle=event.target.closest('[data-tree-toggle]');if(toggle){event.stopImmediatePropagation();const p=toggle.dataset.treeToggle;this.collapsed.has(p)?this.collapsed.delete(p):this.collapsed.add(p);this.renderTree();return;}
  const button=event.target.closest('[data-organize]');if(!button)return;
  event.preventDefault();event.stopImmediatePropagation();if(button.disabled||this.engaged)return;
  if(button.dataset.organize==='refresh'){await this.refresh();return;}
  if(!this.ready)return;
  const v=this.view(),op=button.dataset.organize;
  if(op==='create'||op==='rename')this.nameDialog(op,v);
  else this.moveDialog(op==='move-folder'?{folder_id:v.current}:{episodes:this.selection().map(({id,source_signature})=>({id,source_signature}))},v);
 }
 modal(title,content,label,onSubmit){
  const origin=document.activeElement,d=document.createElement('dialog');this.dialog=d;this.epoch++;
  d.className='data-organization-dialog';d.innerHTML=`<form><h2>${this.esc(title)}</h2>${content}<p class="data-dialog-error" role="status" hidden></p><div class="actions"><button type="button" class="button" data-cancel>取消</button><button class="button primary" type="submit">${label}</button></div></form>`;
  document.body.append(d);d.querySelector('[data-cancel]').onclick=()=>d.close();
  d.addEventListener('cancel',event=>{if(this.busy)event.preventDefault();});
  d.onclose=()=>{this.dialog=null;d.remove();this.render();if(origin?.isConnected)origin.focus({preventScroll:true});else document.querySelector('[data-organize="create"]')?.focus({preventScroll:true});};
  d.querySelector('form').onsubmit=async event=>{event.preventDefault();if(this.busy)return;await onSubmit(d);};d.showModal();d.querySelector('input,select')?.focus();
 }
 nameDialog(op,v){
  const folder=v.allFolders.find(f=>f.path===v.current),revision=this.get().data.organization.revision;
  this.modal(op==='create'?'新建文件夹':'重命名文件夹',`<label for="organization-name">文件夹名称</label><input id="organization-name" name="name" required maxlength="80" autocomplete="off" value="${this.esc(op==='rename'?folder.name:'')}" placeholder="例如 V3_验收"><p>位置：${this.esc(v.breadcrumbs.map(f=>f.name).join(' / ')||'全部文件夹')}</p>`,op==='create'?'创建':'保存',async d=>{
   const name=d.querySelector('input').value.trim();if(!name){d.querySelector('input').focus();return;}
   const body=op==='create'?{operation:op,parent:v.current,name}:{operation:op,folder_id:v.current,name};
   await this.submit(body,revision,d,op==='create'?'文件夹已创建':'名称已保存');
  });
 }
 moveDialog(payload,v){
  const revision=this.get().data.organization.revision,folders=v.allFolders.filter(f=>!payload.folder_id||!f.ancestors.includes(payload.folder_id));
  const options=[{path:'',name:'全部文件夹',ancestors:[]},...folders];
  this.modal('移至文件夹',`<label for="organization-target">目标位置</label><select id="organization-target">${options.map(f=>`<option value="${this.esc(f.path)}">${this.esc(f.path?f.ancestors.map(p=>v.allFolders.find(n=>n.path===p)?.name).join(' / '):f.name)}</option>`).join('')}</select>`, '移动',d=>this.submit({operation:'move',...payload,target:d.querySelector('select').value},revision,d,'分类已保存'));
 }
 async submit(body,revision,d=null,success='分类已保存'){
  if(this.busy)return;this.busy=true;this.epoch++;this.failed=false;this.message='正在保存分类…';
  if(d)d.querySelectorAll('button,input,select').forEach(el=>el.disabled=true);else this.render();
  try{
   if(!await this.login()){this.message='操作未提交';return;}
   const r=await this.api('/api/data/organization',{...body,expected_revision:revision});
   if(this.get().data)this.get().data.organization=r.organization;
   this.message=success;if(d)d.close();
  }catch(error){
   this.failed=true;this.message=error.status?error.message:`保存结果待核对：${error.message}。重新读取后再操作。`;
   if(d){const errorNode=d.querySelector('.data-dialog-error');errorNode.hidden=false;errorNode.textContent=this.message;}
  }finally{
   this.busy=false;
   if(d?.isConnected)d.querySelectorAll('button,input,select').forEach(el=>el.disabled=false);
   this.render();if(!this.dialog)await this.refresh();
  }
 }
 startDrag(event){
  const node=event.target.closest('[data-drag-folder],[data-drag-episode]');if(!node)return;
  if(!this.ready||this.engaged){event.preventDefault();return;}
  const id=node.dataset.dragEpisode,row=this.get().data.episodes.find(e=>e.id===id);
  const selected=this.selection();
  const payload=node.dataset.dragFolder?{folder_id:node.dataset.dragFolder}:{episodes:(selected.some(e=>e.id===id)?selected:[row]).filter(Boolean).map(({id,source_signature})=>({id,source_signature}))};
  this.drag={payload,revision:this.get().data.organization.revision};this.epoch++;
  event.dataTransfer.effectAllowed='move';event.dataTransfer.setData('application/x-f1-data',JSON.stringify(payload));node.classList.add('drag-source');
 }
 validTarget(node){if(!node||!this.drag)return false;const p=node.dataset.folder,id=this.drag.payload.folder_id;if(!id)return true;return p!==id&&!this.view().allFolders.find(f=>f.path===p)?.ancestors.includes(id);}
 over(event){
  if(!this.drag)return;const node=event.target.closest('[data-folder]');document.querySelectorAll('.drag-target').forEach(el=>el.classList.remove('drag-target'));
  if(this.validTarget(node)){event.preventDefault();event.dataTransfer.dropEffect='move';document.querySelectorAll('[data-folder]').forEach(el=>{if(el.dataset.folder===node.dataset.folder)el.classList.add('drag-target');});}
 }
 drop(event){
  const node=event.target.closest('[data-folder]');if(!this.validTarget(node))return;
  event.preventDefault();const {payload,revision}=this.drag,target=node.dataset.folder;this.endDrag();this.submit({operation:'move',...payload,target},revision);
 }
 endDrag(){if(this.drag)this.suppressClick=performance.now()+300;this.drag=null;document.querySelectorAll('.drag-target,.drag-source').forEach(el=>el.classList.remove('drag-target','drag-source'));}
}

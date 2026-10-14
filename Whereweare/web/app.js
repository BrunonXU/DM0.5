(function () {
  'use strict';
  var boardBottom = 0;
  var state = null, nodeMap = {}, viewId = null, detailed = false, returnView = null;
  var camera = { start: null, span: 21, range: 'fortnight' }, loading = false, fingerprint = '', saving = false;
  var pendingState = null, reportDate = null;
  var reportMode = null, reportError = '', reviewSubmitting = false, sourceRequest = 0, toastTimer;
  var readerEpoch = 0, selectedKey = null, opener = null, drag = null;
  var graph = document.getElementById('timeline'), stage = document.getElementById('graphstage');
  var viewport=document.getElementById('plot-viewport'),measureCanvas=document.createElement('canvas');
  var detail = document.getElementById('detail-dialog'), form = document.getElementById('form-dialog');
  var DAY = 86400000, focusId = null, filter = 'all'; // Form compatibility; view state is kept separately.
  function el(id) { return document.getElementById(id); }
  function list(v) { return Array.isArray(v) ? v : []; }
  function esc(v) { return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function clean(v) { return String(v || '').replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/[*`#]/g, '').trim(); }
  function short(v, count) { var s = clean(v); return s.length > count ? s.slice(0, count) + '…' : s; }
  function project(id) { return state && list(state.projects).find(function (p) { return p.id === id; }); }
  function tracking(p) { return Object.assign({tier:p.kind === 'tool' ? 'support' : 'secondary', archived:false, activity:null}, p.tracking || {}); }
  function where() { return state && state.whereweare || {}; }
  function snapshot() { return where().snapshot || {}; }
  function curated(id) { return list(snapshot().projects).find(function (p) { return p.id === id; }) || {}; }
  function mainProject() { return list(state && state.projects).find(function (p) { var t = tracking(p); return t.tier === 'main' && t.activity === 'current' && !t.archived; }); }
  function currentProject() { return project(viewId) || mainProject(); }
  function supporting(p) { return list(state.projects).filter(function (s) { return list(s.supports).indexOf(p.id) !== -1 && !tracking(s).archived; }); }
  function name(p) { return p.name; }
  function color(p) { if(tracking(p).archived)return '#827e75';if(tracking(p).tier==='main')return '#b4493e';var codes=Array.from(p.id).reduce(function(n,c){return n+c.charCodeAt(0);},0);return ['#4f786f','#ad8044','#647c94'][codes%3]; }
  function reportTimezone() { return where().timezone || snapshot().timezone || 'America/New_York'; }
  function timeText(v) { if (!v) return '尚未审阅'; var d = new Date(v); return isNaN(d.getTime()) ? v : new Intl.DateTimeFormat('zh-CN',{timeZone:reportTimezone(),month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(d); }
  function today() { var parts = new Intl.DateTimeFormat('en-US',{timeZone:reportTimezone(),year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date()), v = {}; parts.forEach(function (p) { v[p.type] = p.value; }); return v.year + '-' + v.month + '-' + v.day; }
  function day(v) { if (!/^\d{4}-\d{2}-\d{2}$/.test(v || '')) return null; var ms = Date.parse(v + 'T00:00:00Z'); return isFinite(ms) && new Date(ms).toISOString().slice(0,10) === v ? ms / DAY : null; }
  function iso(d) { return new Date(Math.round(d) * DAY).toISOString().slice(0,10); }
  function dateText(v) { return v ? v.replace(/-/g,'.') : '未排期'; }
  function nodeDateText(n) { return n.kind==='stage'?'当前阶段':n.kind==='checkpoint'?'当前状态 · 非事件日期':n.date ? dateText(n.date) : n.kind === 'next' || n.status === 'planned' ? '未排期' : '日期未记'; }
  function status(v) { return {active:'进行中',doing:'进行中',done:'已完成',planned:'建议 / 待推进',todo:'待推进',blocked:'待解决',verified:'已验证',review:'待验收',unknown:'待确认',document:'已有记录'}[v] || '待确认'; }
  function normalize(v) { return {doing:'active',todo:'planned'}[v] || v || 'planned'; }
  function jobFailed() { var j=where().job||{}; return j.status==='failed' && (!snapshot().reviewed_at || Date.parse(j.finished_at||j.started_at)>=Date.parse(snapshot().reviewed_at)); }
  function busy() { return reviewSubmitting || ['queued','running'].indexOf((where().job || {}).status) !== -1; }
  function toast(message) { clearTimeout(toastTimer); el('toast').textContent = message; el('toast').hidden = false; toastTimer = setTimeout(function () { el('toast').hidden = true; },3000); }
  async function api(path, data, method) { var opts = {cache:'no-store',headers:{}}; if (data) { opts.method = method || 'POST'; opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(data); } var response = await fetch(path,opts), result = await response.json(); if (!response.ok) throw new Error(result.error || '请求失败'); return result; }
  function updateSync(message) {
    var w = where(), job = w.job || {}, stale = (w.freshness || {}).stale, error = message || (state && state.sync || {}).error;
    el('sync-status').textContent = error ? '连接异常 · 保留上次内容' : busy() ? '正在审阅变更…' : jobFailed() ? '审阅失败 · 保留上次内容' : stale ? '文档有变化 · 摘要待更新' : '已审阅 ' + timeText(snapshot().reviewed_at);
    el('snapshot-note').textContent = stale ? (list((w.freshness||{}).content_changed_paths).length?'资料有变化 · 待核对':'部分资料待审阅') : '截至 ' + timeText(snapshot().reviewed_at);
    el('connection-error').hidden = !(error||jobFailed()); el('connection-error').textContent='!';el('connection-error').title=error?'连接异常，查看状态':'更新未完成，查看原因';el('connection-error').setAttribute('aria-label',el('connection-error').title);
    el('update-now').disabled = busy(); el('update-now').innerHTML = busy() ? ({daily:'生成日报…',weekly:'生成周报…',update:'更新进展…'}[(where().job||{}).mode]||'整理进展…') : '更新进度';
    el('update-now').title = !((w.runner || {}).available) ? (w.runner || {}).reason || '可在聊天中问 whereweare？' : '增量审阅项目文档';
  }
  async function refresh(force) {
    if (loading) { if (force) { await new Promise(function (r) { setTimeout(r,80); }); return refresh(true); } return; }
    loading = true;
    try {
      var next = await api('/api/state'), first = !state, w = next.whereweare || {}, snap = w.snapshot || {};
      var fp = JSON.stringify([next.projects,next.timeline_nodes,next.tasks,snap.revision,w.timezone,w.decisions]);
      if (!first && !force && fp !== fingerprint) {
        pendingState=next; el('pending-update').hidden=false;
        state.whereweare=Object.assign({},w,{snapshot:snapshot(),decisions:where().decisions,action_keys:where().action_keys});
      } else { state=next; pendingState=null; el('pending-update').hidden=true; }
      if (camera.start === null) camera=bounds('fortnight');
      updateSync();
      if (first || force) { fingerprint = fp; render(); if(first)resetScroll(); }
      // Only rebuild the review reader when its result/status changed, preserving scrolling.
      var reportFp = JSON.stringify([w.reports,w.job,w.runner,reportError,reportDate]);
      if (reportMode && detail.open && detail.dataset.reportFingerprint !== reportFp) { var scroll = detail.scrollTop; showReport(reportMode); detail.scrollTop = scroll; detail.dataset.reportFingerprint = reportFp; }
    } catch (error) { updateSync(error.message); if (!state) el('project-summary').textContent = '暂时无法连接项目文档'; }
    finally { loading = false; }
  }
  function width() { return Math.max(680,viewport.clientWidth); }
  function geometry() { var w=width(),right=w-202;return {left:22,right:right,now:22+(right-22)*2/3,next:w-174,width:w}; }
  function step() { var g=geometry();return (g.right-g.left)/camera.span; }
  function xDay(d) { return geometry().left+(d-camera.start)*step(); }
  function dateX(v) { return xDay(day(v)); }
  function text(x,y,value,cls,extra) { return '<text x="'+x+'" y="'+y+'" class="'+cls+'" '+(extra||'')+'>'+esc(value)+'</text>'; }
  function group(action,id,cls,html,label) { return '<g role="button" tabindex="0" data-action="'+action+'" data-id="'+esc(id)+'" class="'+cls+'" aria-label="'+esc(label)+'">'+html+'</g>'; }
  function eventNodes(ids) { return list(snapshot().events).filter(function(e){return ids.includes(e.project_id)&&e.lifecycle!=='superseded';}).map(function(e){return Object.assign({},e,{key:'e:'+e.id,kind:'event',event_kind:e.kind,status:e.kind==='blocker'?'blocked':e.kind==='plan'?'planned':'document'});}); }
  function manualNodes(id) { return list(state.timeline_nodes).filter(function(n){return n.project_id===id;}).map(function(n){return Object.assign({},n,{key:'n:'+n.id,kind:'manual'});}); }
  function extras(p) { return list(p.milestones).map(function(m){return Object.assign({},m,{key:'m:'+p.id+':'+m.id,project_id:p.id,kind:'milestone'});}).concat(list(state.tasks).filter(function(t){return t.project_id===p.id&&t.status!=='done';}).map(function(t){return Object.assign({},t,{key:'t:'+t.id,date:t.due_date||null,kind:'task'});})); }
  function sortDate(a,b) { return String(a.date||'').localeCompare(String(b.date||''))||a.key.localeCompare(b.key); }
  function laneNodes(p) { return eventNodes([p.id]).concat(manualNodes(p.id),extras(p)); }
  function ownersFor(p) { return [p].concat(supporting(p).filter(function(s){return tracking(s).activity==='current'||(detailed&&tracking(s).activity===null);}).sort(function(a,b){return a.name.localeCompare(b.name);})); }
  function bounds(range) {
    var now=day(today()),past=range==='month'?30:14,start=now-past,span=past*1.5;
    if(range==='all'&&currentProject()){var dates=ownersFor(currentProject()).flatMap(function(p){return laneNodes(p).filter(function(n){return n.date;}).map(function(n){return day(n.date);});});start=Math.min(start,...dates)-1;span=Math.max((now-start)*1.5,Math.max(now,...dates)+1-start);}
    return {start:start,span:span,range:range};
  }
  function resetScroll() { viewport.scrollLeft=0; }
  function isAway() { return Math.abs(camera.start-bounds(camera.range).start)>.05; }
  function decisionFor(pid,key) { return list(where().decisions).find(function(d){return d.project_id===pid&&d.object_key===key;}); }
  function effectiveNext(pid) { var d=decisionFor(pid,(where().action_keys||{})[pid]);return d&&d.status!=='active'?'':curated(pid).next_step||''; }
  async function saveDecision(pid,key,value) { try {await api('/api/decisions',{project_id:pid,object_key:key,status:value});await refresh(true);if(detail.open)detail.close();toast(value==='paused'?'已记住：暂停这件事':value==='done'?'已记住：完成':'已恢复这件事');}catch(e){toast(e.message);} }
  function staleProject(id) { var fresh=where().freshness||{},paths=list(fresh.content_changed_paths||fresh.changed_paths);return list(curated(id).evidence).some(function(e){return paths.includes(e.path);}); }
  function kindName(n) { return {progress:'成果',maintenance:'改动',decision:'决定',blocker:'待验证',plan:'建议'}[n.event_kind||n.kind]||(n.kind==='manual'?'记录':'里程碑'); }
  function renderArchitecture(p) {
    var owner=list(curated(p.id).stages).length?p:list(state.projects).find(function(s){return list(p.supports).includes(s.id)&&list(curated(s.id).stages).length;});
    var stages=owner?list(curated(owner.id).stages):[];
    el('architecture').hidden=false;
    if(!stages.length){el('architecture').innerHTML='<button class="architecture-empty" data-action="project" data-id="'+esc(p.id)+'">整体链路尚待审阅 <span>查看目标与来源 ↗</span></button>';return;}
    el('architecture').innerHTML='<div class="section-caption">整体链路 <span>点开查看能力与验收条件</span></div><div class="stage-chain">'+stages.map(function(s,i){
      var key='stage:'+owner.id+':'+s.id;nodeMap[key]=Object.assign({},s,{key:key,project_id:owner.id,title:s.label,kind:'stage',detail:s.note});
      return '<button class="stage-item '+esc(s.status)+'" data-action="node" data-id="'+esc(key)+'" aria-label="'+esc(s.label+'，'+s.note)+'"><span class="stage-heading"><span class="stage-symbol" aria-hidden="true">'+({verified:'✓',review:'◐',blocked:'△',unknown:'○'}[s.status]||'○')+'</span><strong>'+esc(s.label)+'</strong><span class="stage-number">0'+(i+1)+'</span></span><span class="stage-note">'+esc(s.note)+'</span></button>';
    }).join('')+'</div>';
  }
  function boxHeight(title,w) {
    var ctx=measureCanvas.getContext('2d');ctx.font='500 18px '+getComputedStyle(document.body).fontFamily;var lines=1,current=0;
    Array.from(clean(title)).forEach(function(c){var cw=ctx.measureText(c).width;if(current+cw>w-12){lines++;current=0;}current+=cw;});return 22+lines*26;
  }
  function labelBox(n,x,y,w,kicker,extraClass,visibleTitle) {
    nodeMap[n.key]=n;var title=visibleTitle===undefined?n.title:visibleTitle,h=boxHeight(title,w);boardBottom=Math.max(boardBottom,y+h);
    return '<foreignObject x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'"><div xmlns="http://www.w3.org/1999/xhtml" class="node-label '+(extraClass||'')+'">'+(kicker?'<div class="node-owner">'+esc(kicker)+'</div>':'')+'<div class="node-title">'+esc(title)+'</div></div></foreignObject>';
  }
  function marker(n,x,y,tint) {
    var shape=n.event_kind||n.kind,svg='<circle class="node-hit" cx="'+x+'" cy="'+y+'" r="17"/>';
    // The center ring is shared by every type, so keyboard targets and date alignment stay stable.
    svg+='<circle class="node-ring" cx="'+x+'" cy="'+y+'" r="4" data-date="'+esc(n.date||'')+'" style="stroke:'+tint+'"/>';
    if(shape==='decision')svg+='<path class="type-marker" d="M'+x+','+(y-7)+' l7,7 -7,7 -7,-7 Z" style="stroke:'+tint+'"/>';
    if(shape==='maintenance')svg+='<rect class="type-marker" x="'+(x-6)+'" y="'+(y-6)+'" width="12" height="12" style="stroke:'+tint+'"/>';
    if(shape==='blocker')svg+='<path class="type-marker" d="M'+x+','+(y-8)+' l8,14 -16,0 Z" style="stroke:'+tint+'"/>';
    return svg;
  }
  function render() {
    if(!state)return;var p=currentProject();nodeMap={};boardBottom=0;var g=geometry(),w=g.width;
    el('repository-nav').innerHTML=repositoryLink();el('project-menu').hidden=!p;el('expand-support').hidden=!p;el('empty-message').hidden=!!p;
    if(!p){el('project-title').textContent='选择当前主线';graph.innerHTML='';graph.style.height='180px';el('lane-labels').innerHTML='';el('architecture').hidden=true;el('empty-message').textContent='在项目库中选择当前主线。';return;}
    focusId=p.id;var c=curated(p.id);el('project-title').textContent=name(p);el('context-label').textContent=(p.platform||'PROJECTS')+' / '+(viewId?'项目详情':'现在');
    el('project-menu').dataset.id=p.id;el('project-summary').textContent=c.headline||'等待审阅项目文档';
    el('back-global').hidden=!detailed&&!viewId;el('expand-support').textContent=detailed?'收起记录 ↖':'展开全部记录 ↗';
    el('project-heading').setAttribute('aria-expanded',String(detailed));el('expand-support').setAttribute('aria-expanded',String(detailed));
    document.querySelectorAll('[data-action="range"]').forEach(function(b){b.setAttribute('aria-pressed',String(b.dataset.range===camera.range));});renderArchitecture(p);
    var rows=[],start=camera.start,end=start+camera.span;
    function inWindow(n){return n.date&&day(n.date)>=start&&day(n.date)<=end;}
    ownersFor(p).forEach(function(owner){
      var all=laneNodes(owner),eligible=all.filter(function(n){return !n.parent_id;}),groups=[];
      eligible.filter(inWindow).sort(sortDate).forEach(function(n){var last=groups[groups.length-1];if(last&&last.date===n.date&&n.kind!=='manual'&&last.items[0].kind!=='manual')last.items.push(n);else groups.push({date:n.date,items:[n]});});
      groups.forEach(function(b){b.items.sort(function(a,b){return ((a.event_kind||a.kind)==='blocker'?-3:(a.event_kind||a.kind)==='progress'?-2:-1)-((b.event_kind||b.kind)==='blocker'?-3:(b.event_kind||b.kind)==='progress'?-2:-1)||(a.importance==='major'?-1:0)-(b.importance==='major'?-1:0);});});
      var maxGroups=Math.max(2,Math.floor((g.now-g.left)/215)),hidden=detailed?[]:groups.slice(0,-maxGroups);if(!detailed)groups=groups.slice(-maxGroups);
      rows.push({p:owner,groups:groups,all:all,previous:eligible.filter(function(n){return n.date&&day(n.date)<start;}).sort(sortDate).pop(),hidden:hidden,root:owner.id===p.id});
      if(detailed){var children=all.filter(function(n){return n.parent_id&&inWindow(n);}),added=new Set();function add(n){if(added.has(n.id))return;added.add(n.id);var parent=all.find(function(a){return a.id===n.parent_id;});if(parent&&parent.parent_id&&inWindow(parent))add(parent);rows.push({p:owner,groups:[{date:n.date,items:[n]}],all:all,parent:parent});}children.forEach(add);}
    });
    var rowTop=56,lines='',nodes='',labels='',laneHTML='',positions={},ownerY={};
    rows.forEach(function(row){
      var tint=color(row.p),labelWidth=Math.min(220,Math.max(170,(g.now-g.left-40)/Math.min(2,Math.max(1,row.groups.length)))),placements=[],rightEdge=g.now-16;
      // Pack labels from the newest backward; retain true marker x and use thin leaders.
      row.groups.slice().reverse().forEach(function(bundle){var px=dateX(bundle.date),lx=Math.min(px-labelWidth/2,rightEdge-labelWidth);var band=0;if(lx<g.left){band=placements.length?Math.max(...placements.map(function(x){return x.band;}))+1:0;rightEdge=g.now-16;lx=Math.max(g.left,Math.min(px-labelWidth/2,rightEdge-labelWidth));}placements.push({bundle:bundle,px:px,lx:lx,band:band});rightEdge=lx-18;});
      var bands=placements.length?1+Math.max(...placements.map(function(x){return x.band;})):1;
      var above=Math.max(54,...placements.map(function(x){return boxHeight(x.bundle.items[0].title,labelWidth);}));
      var cy=rowTop+above+14+(bands-1)*(above+14),cc=curated(row.p.id),blocker=cc.blocker||cc.headline||'',next=effectiveNext(row.p.id),blockWidth=Math.max(152,g.next-g.now-28),nextWidth=w-g.next-8;
      var bottom=Math.max(blocker?boxHeight(blocker,blockWidth)+16:42,next?boxHeight(clean(next).replace(/^建议[：:]?\s*/,''),nextWidth)+16:42,42);
      var rowBottom=cy+bottom+8;
      lines+='<line class="row-separator" x1="0" y1="'+rowBottom+'" x2="'+w+'" y2="'+rowBottom+'"/>';
      lines+='<path class="branch-line '+(row.root?'main':'support')+'" data-project-line="'+esc(row.p.id)+'" stroke="'+tint+'" d="M0,'+cy+' H'+g.now+'"/><path class="future-line" stroke="'+tint+'" d="M'+g.now+','+cy+' H'+(w-10)+'"/>';
      placements.forEach(function(pos){var b=pos.bundle,n=b.items[0];b.items.forEach(function(item){nodeMap[item.key]=item;});if(b.items.length>1)n=Object.assign({},n,{key:'bundle:'+row.p.id+':'+b.date,kind:'bundle',items:b.items});
        var ly=rowTop+8+pos.band*(above+14),kicker=b.date.slice(5).replace('-','.')+' · '+kindName(b.items[0])+(b.items.length>1?' · '+b.items.length+'项':'');
        var h=boxHeight(n.title,labelWidth),ny=cy,html='<path class="node-leader" d="M'+pos.px+','+ny+' V'+(ly+h+3)+' H'+(pos.lx+labelWidth/2)+'"/>'+marker(n,pos.px,ny,tint)+labelBox(n,pos.lx,ly,labelWidth,kicker);
        nodes+=group('node',n.key,'node '+normalize(n.status)+(selectedKey===n.key?' selected':''),html,n.title+'，'+kicker);
        b.items.forEach(function(item){if(item.kind==='manual')positions[item.id]={x:pos.px,y:ny};});
      });
      if(row.parent&&row.parent.date){var parentPos=positions[row.parent.id]||{x:dateX(row.parent.date),y:ownerY[row.p.id]};var childX=dateX(row.groups[0].date);if(parentPos.y!==undefined)lines+='<path class="branch-line explicit-branch" stroke="'+tint+'" d="M'+parentPos.x+','+parentPos.y+' C'+parentPos.x+','+(cy-40)+' '+childX+','+(cy-40)+' '+childX+','+cy+'"/>';}
      if(!row.parent){
        ownerY[row.p.id]=cy;laneHTML+='<div class="lane-name '+(row.root?'main':'')+'" style="top:'+(rowTop+28)+'px;height:'+(rowBottom-rowTop-28)+'px;--lane-color:'+tint+'"><button data-action="focus" data-id="'+esc(row.p.id)+'"><span class="lane-dot" aria-hidden="true"></span>'+esc(cc.lane_label||name(row.p))+'<span class="lane-chevron">›</span></button>';
        if(!row.groups.length&&row.previous){nodeMap[row.previous.key]=row.previous;laneHTML+='<button class="previous-record" data-action="node" data-id="'+esc(row.previous.key)+'"><span>'+esc(row.previous.date.slice(5).replace('-','.'))+' · 上次记录</span>'+esc(row.previous.title)+'</button>';}
        labels+='<circle class="now-dot" cx="'+g.now+'" cy="'+cy+'" r="'+(row.root?6:4)+'" style="fill:'+tint+'"/>';
        if(blocker){var bn={key:'blocker:'+row.p.id,project_id:row.p.id,title:blocker,kind:'checkpoint',status:cc.blocker?'review':'document',evidence:cc.evidence};nodes+=group('node',bn.key,'node checkpoint',labelBox(bn,g.now+13,cy+16,blockWidth,staleProject(row.p.id)?'上次结论 · 待核对':'现在'),blocker+'，当前状态');}
        if(next){var nn={key:'next:'+row.p.id,project_id:row.p.id,title:next,kind:'next',status:'planned',evidence:cc.evidence};nodes+=group('node',nn.key,'node planned','<circle class="next-ring" cx="'+g.next+'" cy="'+cy+'" r="5" style="stroke:'+tint+'"/>'+labelBox(nn,g.next-6,cy+16,nextWidth,'建议','',clean(next).replace(/^建议[：:]?\s*/,'')),next+'，未排期');}
        var remaining=row.all.filter(function(n){return !n.date;}).concat(row.hidden.flatMap(function(b){return b.items;}));list(where().decisions).filter(function(d){return d.project_id===row.p.id&&d.status!=='active';}).forEach(function(d){remaining.push({key:'decision:'+d.object_key,project_id:row.p.id,title:(d.status==='paused'?'已暂停：':'已完成：')+d.title,kind:'decision',status:d.status,object_key:d.object_key,detail:d.note});});
        if(remaining.length){var key='records:'+row.p.id;nodeMap[key]={key:key,project_id:row.p.id,title:'其他记录与路线',kind:'bundle',items:remaining};remaining.forEach(function(n){nodeMap[n.key]=n;});laneHTML+='<button class="lane-more" data-action="node" data-id="'+esc(key)+'">'+remaining.length+' 条记录 ↗</button>';}
        laneHTML+='</div>';
      }
      rowTop=rowBottom;
    });
    var height=Math.max(260,rowTop),markup='<defs><clipPath id="date-clip"><rect x="0" y="48" width="'+g.right+'" height="'+height+'"/></clipPath></defs>';
    markup+='<line class="now-spine" x1="'+g.now+'" y1="43" x2="'+g.now+'" y2="'+height+'"/>';
    markup+=text(g.now,18,isAway()?'当前状态':'今天','today-text','text-anchor="middle"')+text(g.now,37,today().slice(5).replace('-','.'),'axis-label','text-anchor="middle"')+text(g.next-6,28,'接下来 · 未排期','column-heading');
    var tickStep=step()<10?30:step()<30?7:step()<65?3:1;
    for(var d=Math.ceil(start/tickStep)*tickStep;d<=end;d+=tickStep){var x=xDay(d);if(Math.abs(x-g.now)<52||x>g.now-40)continue;markup+=text(x,28,iso(d).slice(5).replace('-','.'),'axis-label','text-anchor="middle"');markup+='<line class="date-grid" x1="'+x+'" y1="48" x2="'+x+'" y2="'+height+'"/>';}
    graph.setAttribute('viewBox','0 0 '+w+' '+height);graph.style.width=w+'px';graph.style.height=height+'px';
    Object.assign(graph.dataset,{dateStart:iso(start),dateEnd:iso(end),focus:detailed?p.id:'',project:p.id,start:camera.start,span:camera.span,range:camera.range,axisLeft:g.left,axisRight:g.right,currentX:g.now,nextX:g.next});
    graph.innerHTML=markup+lines+labels+'<g clip-path="url(#date-clip)">'+nodes.replace(/\u0000/g,'')+'</g>';
    // Current and suggested actions have their own fixed region, outside the date clip.
    Array.from(graph.querySelectorAll('.checkpoint,.planned')).forEach(function(n){graph.appendChild(n);});
    el('lane-labels').innerHTML=laneHTML;el('lane-labels').style.height=height+'px';el('return-now').hidden=!isAway();
  }
  function captureView(){return {viewId:viewId,detailed:detailed,camera:Object.assign({},camera),scrollY:window.scrollY,scrollX:viewport.scrollLeft};}
  function back(){if(returnView){var v=returnView;viewId=v.viewId;detailed=v.detailed;camera=v.camera;returnView=null;render();viewport.scrollLeft=v.scrollX||0;window.scrollTo(0,v.scrollY||0);}else{viewId=null;detailed=false;fit();}}
  function expand(){if(detailed){back();return;}returnView=captureView();detailed=true;render();}
  function enterFocus(id){if(!project(id))return;returnView=captureView();viewId=id;detailed=true;camera=bounds('fortnight');if(detail.open)detail.close();reportMode=null;render();resetScroll();window.scrollTo(0,0);}
  function fit(){camera=bounds('fortnight');detailed=false;returnView=null;render();resetScroll();}
  function openDetail(html){readerEpoch++;if(!detail.open)opener=document.activeElement;reportMode=null;detail.innerHTML=html;if(!detail.open)detail.showModal();}
  function closeButton(){return '<button type="button" class="close" data-action="close" aria-label="关闭">×</button>';}
  function drawerHeader(label){return '<div class="drawer-top"><span class="drawer-kicker">'+esc(label)+'</span>'+closeButton()+'</div>';}
  function sourceButton(path){return '<button class="source-link" data-action="source" data-path="'+esc(path)+'">'+esc(path)+' ↗</button>';}
  function evidenceHtml(evidence){var entries=list(evidence);return entries.length?'<details class="evidence"><summary>查看依据 · '+entries.length+'</summary>'+entries.map(function(e){return '<p>'+sourceButton(typeof e==='string'?e:e.path)+(e.line?' <small>L'+esc(e.line)+'</small>':'')+'</p>';}).join('')+'</details>':'';}
  function repositoryLink(){var url=where().workspace_repository_url;try{var parsed=new URL(url);if(parsed.protocol==='https:'&&parsed.hostname==='github.com'&&!parsed.username&&!parsed.password)return '<a class="repository-link" href="'+esc(parsed.href)+'" target="_blank" rel="noopener noreferrer">Projects · GitHub <span aria-hidden="true">↗</span></a>';}catch(_){}return '';}
  function workspaceLinks(){return '<div class="workspace-links">'+repositoryLink()+'<button data-action="sync">更新状态 <span aria-hidden="true">↗</span></button></div>';}
  function showSync(){var w=where(),j=w.job||{},fresh=w.freshness||{};openDetail(drawerHeader('Whereweare')+'<h2 id="detail-title">更新状态</h2><p class="detail-copy">'+esc(el('sync-status').textContent)+'</p><div class="detail-meta">截至 '+esc(timeText(snapshot().reviewed_at))+'</div>'+(jobFailed()?'<p class="form-error">'+esc(j.error||'本次更新未完成')+'</p>':'')+'<p class="detail-copy">'+esc(busy()?'正在整理项目变化。':fresh.stale?'文档有变化，等待更新摘要。':'当前显示最近一次审阅结果。')+'</p>'+repositoryLink()+'<div class="update-settings"><label><input id="auto-review-setting" type="checkbox"'+((w.automation||{}).enabled?' checked':'')+'>自动更新当前项目</label><p class="report-notice">变更稳定两分钟后审阅，每天最多 '+esc((w.automation||{}).daily_limit||8)+' 次。关闭后仍可手动更新。</p></div>');}
  function showLibrary(){var groups=[['当前','current'],['早期构想','idea'],['暂时搁置','paused'],['其他项目',null],['已归档','archived']];var html=drawerHeader('PROJECTS')+'<h2 id="detail-title">项目库</h2>';html+=workspaceLinks();groups.forEach(function(g){var ps=list(state.projects).filter(function(p){var t=tracking(p);return g[1]==='archived'?t.archived:!t.archived&&t.activity===g[1];});if(!ps.length)return;html+='<section class="library-group"><h3>'+g[0]+'</h3>'+ps.map(function(p){return '<button class="library-item '+(tracking(p).archived?'archived':'')+'" data-action="focus" data-id="'+esc(p.id)+'"><span class="library-name">'+esc(name(p))+'</span><small>'+esc(p.platform==='shared'?'共享':p.platform||'未分类')+'</small><span>↗</span></button>';}).join('')+'</section>';});openDetail(html);}
  function showProject(id){var p=project(id);if(!p)return;var c=curated(id),t=tracking(p),html=drawerHeader((p.platform||'共享')+' / 项目设置')+'<h2 id="detail-title">'+esc(name(p))+'</h2><p class="project-headline">'+esc(c.headline||'等待审阅')+'</p>';if(c.goal)html+='<div class="concise-row"><span>目标'+(c.goal_status==='proposed'?' · 建议':'')+'</span><p>'+esc(c.goal)+'</p></div>';html+='<div class="drawer-actions"><button class="button" data-action="new" data-id="'+esc(id)+'">补一条记录</button></div><h3>投入状态</h3><div class="activity-choices">'+[['current','当前'],['idea','构想'],['paused','暂停'],['','未分类']].map(function(v){return '<button class="button" data-action="activity" data-id="'+esc(id)+'" data-value="'+v[0]+'" aria-pressed="'+(t.activity===(v[0]||null))+'">'+v[1]+'</button>';}).join('')+'</div><div class="tracking-controls"><button class="button" id="toggle-main" data-action="tier" data-id="'+esc(id)+'" data-tier="'+(t.tier==='main'?'secondary':'main')+'">'+(t.tier==='main'?'改为支线':'设为主线')+'</button><button class="button" id="archive-project" data-action="archive" data-id="'+esc(id)+'" data-archived="'+(!t.archived)+'">'+(t.archived?'恢复项目':'暂时归档')+'</button></div>';html+=evidenceHtml(c.evidence&&c.evidence.length?c.evidence:list(p.source_paths).map(function(path){return {path:path};}));openDetail(html);}
  async function updateTracking(id,changes){var ticket=readerEpoch;try{await api('/api/projects/'+encodeURIComponent(id)+'/tracking',changes,'PATCH');await refresh(true);if(detail.open&&ticket===readerEpoch)showProject(id);}catch(error){toast(error.message);}}
  function showNode(key){var n=nodeMap[key],p=n&&project(n.project_id);if(!p)return;if(n.kind==='stage'){
    var cc=curated(p.id),supportIds=list(n.support_ids),html=drawerHeader(name(p)+' / 整体链路')+'<h2 id="detail-title">'+esc(n.title)+'</h2><div class="detail-meta">'+esc(status(n.status))+'</div><div class="concise-row"><span>已知能力与验证范围</span><p>'+esc(n.capability||n.note||n.detail)+'</p></div><div class="concise-row"><span>验收条件</span><p>'+esc(n.exit_criteria||'尚未单独记录验收条件，需要结合来源核对。')+'</p></div>';
    if(supportIds.length)html+='<div class="concise-row"><span>由这些工作支撑</span><div class="stage-supports">'+supportIds.filter(function(id){return project(id);}).map(function(id){return '<button data-action="focus" data-id="'+esc(id)+'">'+esc(name(project(id)))+' ↗</button>';}).join('')+'</div></div>';
    html+=evidenceHtml(n.evidence);openDetail(html);return;
  }if(n.kind==='bundle'){openDetail(drawerHeader(name(p))+'<h2 id="detail-title">'+esc(n.date?dateText(n.date)+' · 变化':n.title)+'</h2>'+n.items.map(function(item){nodeMap[item.key]=item;return '<button class="record-item" data-action="node" data-id="'+esc(item.key)+'"><small>'+esc(nodeDateText(item))+'</small><span>'+esc(item.title)+'</span><span>↗</span></button>';}).join(''));return;}selectedKey=key;graph.querySelectorAll('.selected').forEach(function(e){e.classList.remove('selected');});var selected=Array.from(graph.querySelectorAll('[data-action="node"]')).find(function(e){return e.dataset.id===key;});if(selected)selected.classList.add('selected');var c=curated(p.id),html=drawerHeader(name(p)+' / '+(n.kind==='next'?'下一步':n.kind==='checkpoint'?'当前待解决':n.kind==='stage'?'整体链路':'变化记录'))+'<h2 id="detail-title">'+esc(n.title)+'</h2><div class="detail-meta"><span>'+esc(nodeDateText(n))+'</span><span>'+esc(status(normalize(n.status)))+'</span></div>';if(n.notes||n.detail)html+='<div class="concise-row"><span>变化与影响</span><p>'+esc(n.notes||n.detail)+'</p></div>';if(effectiveNext(p.id)&&n.kind!=='next')html+='<div class="concise-row"><span>下一步 · 建议</span><p>'+esc(effectiveNext(p.id))+'</p></div>';var decisionKey=n.kind==='next'?(where().action_keys||{})[p.id]:n.kind==='event'?'event:'+n.id:n.object_key;if(decisionKey){var decision=decisionFor(p.id,decisionKey);html+='<div class="memory-actions">'+[['paused','暂停这件事'],['done','标记完成'],['active','恢复']].filter(function(v){return !decision?v[0]!=='active':v[0]!==decision.status;}).map(function(v){return '<button class="button" data-action="decision" data-id="'+esc(p.id)+'" data-key="'+esc(decisionKey)+'" data-value="'+v[0]+'">'+v[1]+'</button>';}).join('')+'</div>';}if(n.kind==='manual')html+='<div class="drawer-actions"><button class="button" data-action="branch" data-id="'+esc(n.id)+'">从这里分叉</button><button class="button" data-action="edit" data-id="'+esc(n.id)+'">编辑记录</button></div>';html+=evidenceHtml(n.evidence|| (n.source_path?[{path:n.source_path}]:[]));openDetail(html);}
  function reportList(title,values,limit){return list(values).length?'<section class="report-section"><h3>'+title+'</h3><ul>'+list(values).slice(0,limit).map(function(v){return '<li>'+esc(typeof v==='string'?v:v.title||v.text||'')+'</li>';}).join('')+'</ul></section>':'';}
  function reportPeriod(mode) { var date=reportDate||today(),value=day(date),weekday=new Date(value*DAY).getUTCDay();return mode==='weekly'?iso(value-(weekday+6)%7):date; }
  function showReport(mode){
    var w=where(),period=reportPeriod(mode),entries=list(w.reports).filter(function(r){return r.mode===mode&&r.period_start===period&&r.timezone===reportTimezone();}),s=entries[entries.length-1],r=s&&s.report||{},job=w.job||{},sameJob=job.mode===mode&&job.period_start===period&&job.timezone===reportTimezone(),running=sameJob&&busy();
    var html=drawerHeader('回顾')+'<div class="report-tabs" role="tablist">'+[['daily','今日'],['weekly','本周']].map(function(v){return '<button role="tab" aria-selected="'+(mode===v[0])+'" data-action="report" data-mode="'+v[0]+'">'+v[1]+'</button>';}).join('')+'</div><h2 id="detail-title">'+(mode==='weekly'?'这一周的进展':'这一天的进展')+'</h2><label class="report-notice" for="report-date">选择日期'+(mode==='weekly'?'所在周':'')+'</label><br><input class="report-date" id="report-date" type="date" value="'+esc(reportDate||today())+'" max="'+today()+'"><p class="report-timestamp">'+esc(period+(mode==='weekly'?' — '+iso(day(period)+6):'')+' · '+reportTimezone())+'</p>';
    if(s){html+='<p class="project-headline" id="report-headline">'+esc(r.headline||'暂无新进展')+'</p><p class="report-timestamp">资料截至 '+esc(timeText(s.fact_reviewed_at||s.reviewed_at))+'</p>'+reportList('核心变化',r.changes,3)+reportList(mode==='weekly'?'下周建议':'明日建议',r.next,mode==='weekly'?3:1)+reportList('待解决',r.risks,1);}
    else html+='<p class="report-empty" id="report-headline">本期尚未生成</p>';
    if(reportError||(sameJob&&job.status==='failed'))html+='<p id="review-error" class="form-error">'+esc(reportError||job.error)+' · 已保留已有内容。</p>';
    if((w.freshness||{}).stale)html+='<p class="report-notice">资料有变化，当前回顾使用已审阅版本。<button data-action="review" data-mode="update">先更新进度 ↗</button></p>';
    if(!((w.runner||{}).available))html+='<p class="report-notice">'+esc((w.runner||{}).reason||'审阅暂不可用')+'</p>';
    var sameVersion=s&&s.base_revision===snapshot().revision&&s.decisions_fingerprint===w.decisions_fingerprint;
    html+='<div class="drawer-actions"><button class="button primary" id="run-review" data-action="review" data-mode="'+mode+'"'+(busy()?' disabled':'')+'>'+(running?'正在生成本期回顾…':sameVersion?'查看当前版本':s?'生成新版回顾':mode==='weekly'?'生成本周回顾':'生成今日回顾')+'</button></div>';
    openDetail(html);reportMode=mode;
  }
  async function runReview(mode){
    if(busy())return;reviewSubmitting=true;reportError='';if(mode!=='update')showReport(mode);updateSync();
    try{var result=await api('/api/review/run',Object.assign({mode:mode},mode==='update'?{}:{date:reportDate||today()}));await refresh(true);if(result.cached)toast('已是当前资料版本');}
    catch(error){reportError=error.message;if(mode==='update')toast(error.message);}
    finally{reviewSubmitting=false;if(reportMode===mode&&detail.open)showReport(mode);updateSync();}
  }
  function manualNode(id) { return list(state.timeline_nodes).find(function (n) { return n.id === id; }); }
  function showForm(projectId, parentId, nodeId) {
    if (!state) return;
    var n = nodeId ? manualNode(nodeId) : null, parent = parentId ? manualNode(parentId) : null;
    var pid = n ? n.project_id : parent ? parent.project_id : projectId || focusId || (filter !== 'all' && filter !== 'tools' ? filter : list(state.projects)[0] && state.projects[0].id);
    if (!pid) return toast('先登记一条项目主线');
    var p = project(pid), title = n ? '编辑节点' : parent ? '长出一条分支' : '添加节点';
    form.innerHTML = '<form id="node-form" data-node-id="' + esc(nodeId || '') + '" data-parent-id="' + esc(parentId || '') + '"><div class="form-heading"><h2 id="form-title">' + title + '</h2>' + closeButton() + '</div><p class="form-subtitle">' + esc(parent ? name(p) + ' · 从「' + short(parent.title, 25) + '」分出' : '让下一步出现在你的主线上') + '</p><div class="field"><label for="node-project">主线</label><select id="node-project" name="project_id"' + (n || parent ? ' disabled' : '') + '>' + list(state.projects).map(function (item) { return '<option value="' + esc(item.id) + '"' + (item.id === pid ? ' selected' : '') + '>' + esc(name(item)) + '</option>'; }).join('') + '</select></div><div class="field"><label for="node-title">节点名称</label><input id="node-title" name="title" placeholder="例如：完成第一轮分拣评测" maxlength="120" required value="' + esc(n && n.title || '') + '" autocomplete="off"></div><div class="field-row"><div class="field"><label for="node-date">日期 <span>· 可留空</span></label><input id="node-date" name="date" type="date" value="' + esc(n && n.date || '') + '"' + (parent && parent.date ? ' min="' + esc(parent.date) + '"' : '') + '></div><div class="field"><label for="node-status">状态</label><select id="node-status" name="status">' + ['planned', 'active', 'done', 'blocked'].map(function (s) { return '<option value="' + s + '"' + ((n && n.status || 'planned') === s ? ' selected' : '') + '>' + status(s) + '</option>'; }).join('') + '</select></div></div><details' + (n && n.notes ? ' open' : '') + '><summary>补充说明</summary><div class="field"><label class="field-label" for="node-notes">详情</label><textarea id="node-notes" name="notes" maxlength="5000" placeholder="相关记录、完成条件…">' + esc(n && n.notes || '') + '</textarea></div></details><p id="form-error" class="form-error" role="alert" hidden></p><div class="form-actions"><button type="button" class="button" data-action="close">取消</button><button type="submit" class="button primary" id="form-submit">' + (n ? '保存' : parent ? '添加分支' : '添加节点') + '</button></div></form>';
    if (detail.open) detail.close(); if (!form.open) form.showModal();
    setTimeout(function () { el('node-title').focus(); }, 30);
  }
  async function saveForm(event) {
    if (event.target.id !== 'node-form') return;
    event.preventDefault(); if (saving) return;
    saving = true; el('form-submit').disabled = true; el('form-error').hidden = true;
    var nodeId = event.target.dataset.nodeId, parentId = event.target.dataset.parentId;
    var data = { title: el('node-title').value.trim(), date: el('node-date').value || null, status: el('node-status').value, notes: el('node-notes').value.trim() };
    if (!nodeId) { data.project_id = el('node-project').value; data.parent_id = parentId || null; }
    try { await api('/api/timeline/nodes' + (nodeId ? '/' + encodeURIComponent(nodeId) : ''), data, nodeId ? 'PATCH' : 'POST'); form.close(); await refresh(true); toast(nodeId ? '节点已保存' : parentId ? '新分支已添加' : '节点已添加'); }
    catch (error) { el('form-error').textContent = error.message; el('form-error').hidden = false; }
    finally { saving = false; if (el('form-submit')) el('form-submit').disabled = false; }
  }
  async function showSource(path) {
    var ticket = ++sourceRequest;
    openDetail(drawerHeader('SOURCE / 来源原文') + '<h2 id="detail-title">' + esc(path.split('/').pop()) + '</h2><p class="detail-copy">' + esc(path) + '</p><pre class="source-content" id="source-content">读取中…</pre>');
    try { var result = await api('/api/document?path=' + encodeURIComponent(path)); if (ticket === sourceRequest && el('source-content')) el('source-content').textContent = result.content || ''; }
    catch (error) { if (ticket === sourceRequest && el('source-content')) el('source-content').textContent = error.message; }
  }
  function action(target){var a=target.dataset.action,id=target.dataset.id;
    if(a==='close'){reportMode=null;var dialog=target.closest('dialog');if(dialog)dialog.close();}
    else if(a==='home'){viewId=null;detailed=false;returnView=null;fit();if(detail.open)detail.close();}
    else if(a==='apply-update'){if(pendingState){state=pendingState;pendingState=null;fingerprint=JSON.stringify([state.projects,state.timeline_nodes,state.tasks,snapshot().revision,where().timezone,where().decisions]);el('pending-update').hidden=true;render();updateSync();}}
    else if(a==='decision')saveDecision(id,target.dataset.key,target.dataset.value);else if(a==='library')showLibrary();else if(a==='project')showProject(id);else if(a==='node')showNode(id);
    else if(a==='expand')expand();else if(a==='focus')enterFocus(id);else if(a==='back')back();else if(a==='sync')showSync();else if(a==='pan'){camera.start+=Number(target.dataset.delta);render();}
    else if(a==='new')showForm(id);else if(a==='branch'){var n=manualNode(id);if(n)showForm(n.project_id,id);}else if(a==='edit')showForm(null,null,id);
    else if(a==='tier')updateTracking(id,{tier:target.dataset.tier});else if(a==='archive')updateTracking(id,{archived:target.dataset.archived==='true'});else if(a==='activity')updateTracking(id,{activity:target.dataset.value||null});
    else if(a==='report'){reportError='';showReport(target.dataset.mode);}else if(a==='review')runReview(target.dataset.mode);
    else if(a==='source')showSource(target.dataset.path);else if(a==='fit')fit();else if(a==='today'){camera=bounds(camera.range);render();resetScroll();}else if(a==='range'){camera=bounds(target.dataset.range);render();resetScroll();}
  }
  document.addEventListener('click',function(event){var target=event.target.closest('[data-action]');if(target){event.preventDefault();action(target);}});
  graph.addEventListener('keydown',function(event){var target=event.target.closest('[data-action]');if(target&&(event.key==='Enter'||event.key===' ')){event.preventDefault();action(target);}});
  document.addEventListener('submit',saveForm);
  document.addEventListener('change',async function(event){if(event.target.id==='auto-review-setting'){try{await api('/api/review/automation',{enabled:event.target.checked});await refresh(true);toast('自动更新设置已保存');}catch(e){event.target.checked=!event.target.checked;toast(e.message);}}if(event.target.id==='report-date'){reportDate=event.target.value||today();reportError='';showReport(reportMode||'daily');}});
  
  function panPixels(pixels){camera.start+=pixels/step();render();}
  graph.addEventListener('pointerdown',function(event){
    if(event.pointerType==='touch'||event.target.closest('[data-action]')||event.button!==0)return;
    drag={id:event.pointerId,x:event.clientX,start:camera.start};graph.setPointerCapture(event.pointerId);graph.classList.add('dragging');
  });
  graph.addEventListener('pointermove',function(event){if(drag&&drag.id===event.pointerId){camera.start=drag.start-(event.clientX-drag.x)/step();render();}});
  function releasePointer(){drag=null;graph.classList.remove('dragging');}
  graph.addEventListener('pointerup',releasePointer);graph.addEventListener('pointercancel',releasePointer);
  viewport.addEventListener('wheel',function(event){
    if(event.ctrlKey||event.metaKey)return;
    if(!event.shiftKey&&Math.abs(event.deltaX)<=Math.abs(event.deltaY))return;
    event.preventDefault();var unit=event.deltaMode===1?16:event.deltaMode===2?viewport.clientWidth:1;
    panPixels((event.deltaX||event.deltaY)*unit);
  },{passive:false});
  viewport.addEventListener('keydown',function(event){
    if(event.target!==viewport||!['ArrowLeft','ArrowRight','Home'].includes(event.key))return;
    event.preventDefault();if(event.key==='Home'){camera=bounds(camera.range);render();resetScroll();}
    else panPixels(event.key==='ArrowRight'?100:-100);
  });
  viewport.addEventListener('scroll',function(){if(state){el('return-now').hidden=!isAway();}},{passive:true});
  window.addEventListener('resize',function(){if(state){render();resetScroll();}});
  [detail,form].forEach(function(dialog){dialog.addEventListener('close',function(){if(dialog===detail&&!detail.open){readerEpoch++;reportMode=null;sourceRequest++;selectedKey=null;if(opener&&opener.isConnected)opener.focus();else el('project-heading').focus();graph.querySelectorAll('.selected').forEach(function(e){e.classList.remove('selected');});}});dialog.addEventListener('click',function(event){if(event.target===dialog){var r=dialog.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)dialog.close();}});});
  refresh();setInterval(function(){if(!document.hidden)refresh();},2500);document.addEventListener('visibilitychange',function(){if(!document.hidden)refresh();});
}());

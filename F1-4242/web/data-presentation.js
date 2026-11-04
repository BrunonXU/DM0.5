// Read-only presentation helpers. Metrics use paired review samples, never zero-fill.
const finite=Number.isFinite;
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const value=x=>finite(x)?x.toFixed(2):'—';
export function previewState(e){
 if(e.videos?.length)return ['可播放','good'];
 const states={waiting_config:'等待服务器配置',transferring:'传输中',encoding:'生成中',waiting_server:'等待服务器',leased:'传输 / 生成中',downloading:'传输中',rendering:'生成中',uploading:'视频回传中',failed:'生成失败',error:'生成失败',paused:'等待采集结束',ready:'等待视频索引'};
 return [states[e.preview_status]||(e.recording_status==='recovery'?'原件待排查':'视频未就绪'),['failed','error'].includes(e.preview_status)?'warn':'neutral'];
}
export function pendingVideoHTML(e){const [status,tone]=previewState(e);return `<div class="video-pending notice ${tone==='warn'?'warn':''}" data-video-pending><h3>${esc(status)}</h3><p>${e.recording_status==='recovery'?'录制尚未完整提交，原件保留供排查。':'已保留原始录制；视频就绪后会自动显示。'}</p><p>等待期间可填写审核备注。${e.preview_status==='waiting_server'?'采集占用时，服务器传输会等待空闲。':''}</p><button class="button small" data-click="refresh-data">刷新视频状态</button></div>`;}
export function channelIdentity(name,unit=''){
 let m=String(name).match(/^arm_([lr])_j([1-7])$/)||String(name).match(/^([左右])臂\s*J([1-7])$/i);
 if(m)return {key:`arm_${['l','左'].includes(m[1])?'l':'r'}_j${m[2]}`,label:`${['l','左'].includes(m[1])?'左':'右'}臂 J${m[2]}`,kind:'angle'};
 m=String(name).match(/^(head|body)_j([1-8])$/);if(m)return {key:name,label:`${m[1]==='head'?'头部':'身体'} J${m[2]}`,kind:'angle'};
 if(/gripper|夹爪/.test(name)){const left=/left|左/.test(name);return {key:'gripper_'+(left?'left':'right'),label:left?'左夹爪':'右夹爪',kind:'gripper'};}
 return {key:name,label:name,kind:['deg','degree','°'].includes(unit)?'angle':'other'};
}
export function jointMetrics(data){
 const names=data.channel_names||[],times=data.timestamps_s||[];
 const channels=names.map((name,j)=>{const id=channelIdentity(name,data.units?.[j]);let count=0,sum=0,max=null,maxTime=null,observed=0,commanded=0;
  for(let i=0;i<times.length;i++){const a=data.action?.[i]?.[j],s=data.state?.[i]?.[j];if(finite(s))observed++;if(finite(a))commanded++;if(!finite(a)||!finite(s)||!finite(times[i]))continue;const error=Math.abs(a-s);sum+=error;count++;if(max===null||error>max){max=error;maxTime=times[i];}}
  return {...id,index:j,count,observed,commanded,mean:count?sum/count:null,max,maxTime,unit:data.units?.[j]||''};
 });
 const angular=channels.filter(c=>c.kind==='angle'&&['deg','degree','°'].includes(c.unit)),paired=angular.filter(c=>c.count);
 const aggregate=rows=>{const valid=rows.filter(c=>c.count);return valid.length?valid.reduce((s,c)=>s+c.mean,0)/valid.length:null;};
 const maximum=paired.reduce((a,c)=>!a||c.max>a.max?c:a,null);
 return {channels,mean:aggregate(paired),maximum,count:paired.length,total:Math.max(22,angular.length),left:aggregate(paired.filter(c=>c.key.startsWith('arm_l'))),right:aggregate(paired.filter(c=>c.key.startsWith('arm_r')))};
}
export function jointOverviewHTML(data){
 const m=jointMetrics(data),by=new Map(m.channels.map(c=>[c.key,c])),arm=(side)=>Array.from({length:7},(_,i)=>by.get(`arm_${side}_j${i+1}`)||{label:`${side==='l'?'左':'右'}臂 J${i+1}`,mean:null,index:null});
 const scale=Math.max(.1,...m.channels.filter(c=>c.kind==='angle').map(c=>c.mean||0));
 const row=c=>`<button type="button" class="joint-overview-row" ${c.index===null?'disabled':`data-joint-channel="${c.index}"`} title="${esc(c.label)}：${finite(c.mean)?'查看曲线':c.observed?'缺少可配对命令':'没有可比较数据'}"><span>${esc(c.label.split(' ').at(-1))}</span><i><b style="width:${finite(c.mean)?Math.max(1,c.mean/scale*100):0}%"></b></i><span>${value(c.mean)}${finite(c.mean)?'°':''}</span></button>`;
 const max=m.maximum;
 return `<div class="joint-overview-heading"><h3>所有关节 · 跟随概览</h3><small>录制内：命令与实测</small></div><div class="joint-overview-kpis"><div><span>角度关节平均偏差</span><strong>${value(m.mean)}${finite(m.mean)?'<small>°</small>':''}</strong><small>有效关节等权平均</small></div><div><span>最大采样偏差</span><strong>${value(max?.max)}${max?'<small>°</small>':''}</strong><small>${max?`<button type="button" data-metric-time="${max.maxTime}" data-metric-channel="${max.index}">${esc(max.label)} · ${max.maxTime.toFixed(2)}s ↗</button>`:'暂无有效配对'}</small></div><div><span>参与比较 / 角度关节</span><strong>${m.count}<small>/ ${m.total}</small></strong><small>缺失或无有效配对不计入</small></div></div><div class="joint-overview-arms">${[['l','左臂',m.left],['r','右臂',m.right]].map(([s,n,v])=>`<div><div class="joint-arm-heading"><strong>${n}</strong><span>平均 ${value(v)}${finite(v)?'°':''}</span></div>${arm(s).map(row).join('')}</div>`).join('')}</div><div class="joint-overview-context">${[['head',3,'头部'],['body',5,'身体']].map(([key,total,label])=>{const rows=Array.from({length:total},(_,i)=>by.get(`${key}_j${i+1}`)),count=rows.filter(c=>c?.count).length;return `<span>${label} ${count} / ${total} 轴有有效配对${count?'':' · '+(rows.some(c=>c?.observed)?'仅实测':'未记录')}</span>`;}).join('')}</div><div class="joint-overview-grippers"><span>夹爪 · 平均偏差</span>${['left','right'].map(s=>{const c=by.get('gripper_'+s),percent=['%','percent','0..100'].includes(c?.unit);return `<span>${s==='left'?'左':'右'} <b>${value(c?.mean)}${finite(c?.mean)?' '+(percent?'pp':esc(c.unit)):''}</b></span>`;}).join('')}<span>开度单列，不混入角度平均</span></div><p class="joint-overview-note">按审阅采样点计算平均绝对差；缺失不填零。点选关节可展开曲线。</p><details class="joint-stat-table"><summary>全部通道统计</summary><div class="table-scroll"><table><thead><tr><th>通道</th><th>平均绝对差</th><th>有效配对 / 采样点</th></tr></thead><tbody>${m.channels.map(c=>`<tr><td>${esc(c.label)}</td><td>${value(c.mean)} ${esc(c.kind==='gripper'&&['%','percent','0..100'].includes(c.unit)?'pp':c.unit)}</td><td>${c.count} / ${data.timestamps_s.length}</td></tr>`).join('')}</tbody></table></div></details>`;
}
export function timestampNotesHTML(notes){
 const matches=[...String(notes||'').matchAll(/^\[(?:视频\s*)?(\d+(?:\.\d+)?)s\]\s*(.*)$/gm)].slice(0,100);
 return matches.length?matches.map(m=>`<div class="data-time-note"><button type="button" data-note-seek="${Number(m[1])}">${Number(m[1]).toFixed(2)}s</button><span>${esc(m[2]||'时间点备注')}</span></div>`).join(''):'<p class="section-note">播放时点击「记时间点」，可快速返回对应画面。</p>';
}

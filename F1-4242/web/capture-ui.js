// Shared visual vocabulary only. Selecting a guide never starts robot control.
import {startInstructions} from './hil-instructions.js';
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths={pad:'<path d="M7 7h10c3 0 5 12 3 13-2 1-4-4-5-4H9c-1 0-3 5-5 4C2 19 4 7 7 7Z"/><path d="M7 10v5m-2-2.5h4m6-1h.1m2 2h.1"/>',headset:'<path d="M3 8h18v10h-5l-2-3h-4l-2 3H3zM6 8V5h12v3"/>',link:'<path d="m9 15 6-6m-5-3 2-2a5 5 0 0 1 7 7l-2 2M14 18l-2 2a5 5 0 0 1-7-7l2-2"/>',person:'<circle cx="12" cy="6" r="4"/><path d="M4 22v-4a8 8 0 0 1 16 0v4Z"/>',arm:'<circle cx="7" cy="15" r="3"/><circle cx="16" cy="5" r="3"/><path d="m8 12 5-5m5 1 3 4-2 3m-3-2 3 2M5 18v3h8v-3"/>',cube:'<path d="m12 2 10 5v10l-10 5-10-5V7Z M2 7l10 5 10-5M12 12v10"/>',screen:'<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M12 17v4m-5 0h10"/>',book:'<path d="M12 5v16M12 5C7 1 2 3 2 3v16s5-2 10 2c5-4 10-2 10-2V3s-5-2-10 2Z"/>',pulse:'<path d="M2 12h5l3-8 4 16 3-8h5"/>',check:'<path d="m5 12 4 4L19 6"/>',arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>'};
export const captureIcon=name=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name]||paths.link}</svg>`;
export const hilModes=[
 {id:'play',route:'play',title:'自由接入',icon:'link',short:'不录制',description:'直接遥操，不录制',detail:'适合联调与试操作',action:'查看接入步骤'},
 {id:'demonstration',route:'demonstration',title:'人工示教',icon:'person',short:'保存示范',description:'全程人工，保存示范',detail:'手柄操作 · 按条保存',action:'查看示教步骤'},
 {id:'stageb1250',route:'policy',title:'DAgger 纠正',icon:'arm',short:'人工纠正',description:'策略执行，人工纠正',detail:'接管后人工完成本条',action:'查看纠正步骤'},
 {id:'rlt400',route:'policy',title:'局部 RLT',icon:'cube',short:'局部任务',description:'VLA 接近，局部任务学习',detail:'局部成功确认 · 整条封存',action:'进入连接与启动'}
];
export function hilModeCards(routes=[],selected='',compact=false,view='connect'){
 return hilModes.map(m=>{const r=routes.find(r=>r.id===m.route),on=selected===m.id;return `<a class="${compact?'capture-mode':'card hil-route-card'} route-${m.id}" href="#hil?route=${m.route}${m.route==='policy'?'&amp;model='+m.id:''}&amp;view=${view}" aria-current="${on?'page':'false'}">${captureIcon(m.icon)}<span class="capture-mode-copy"><strong>${m.title}</strong><small>${compact?m.short:m.description}</small></span>${on?'<span class="capture-selected">'+captureIcon('check')+'</span>':''}${compact?'':`<p class="capture-mode-detail">${m.detail}</p>${r&&!r.ready?`<small class="hil-route-badge">${esc(r.reason||'等待后端安装')}</small>`:''}<span class="capture-mode-action">${m.action}${captureIcon('arrow')}</span>`}</a>`;}).join('');
}
export function hilOperationHTML(panel){
 const key=panel.route==='policy'?panel.guideModel==='rlt400'?'rlt':'policy':panel.route;
 const steps=key==='rlt'?[['START','开始本条'],['→','进入局部'],['↑','确认局部成功'],['Y','复位并封存']]:key==='play'?[['START','进入人工'],['RB','按住控臂'],['松 RB','保持姿态']]:[['START','开始本条'],['RB','按住控臂'],['松 RB','等夹爪完成'],['↑','保存本条']];
 return `<section class="capture-operation"><div class="capture-operation-title"><strong>本条操作</strong><small>实体手柄</small></div><div class="capture-operation-steps">${steps.map(([k,t])=>`<span><kbd>${k}</kbd>${t}</span>`).join('')}</div><details><summary>完整按键说明</summary><p>${esc(startInstructions[key])}</p></details></section>`;
}
export function connectionRows(rows){return rows.map(([name,label,tone,icon])=>`<div class="capture-connection-row">${captureIcon(icon)}<span>${esc(name)}</span><strong class="${tone||''}"><i></i>${esc(label)}</strong></div>`).join('');}

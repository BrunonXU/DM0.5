import {startInstructions,homeInstruction,recoveryInstruction} from './hil-instructions.js';
/* F710 layout reference: Logitech F710 Quick Start Guide, 620-002923.007.
 * Original interactive SVG, not a copied product photograph. Browser teaching only.
 * https://www.logitech.com/assets/48300/f710-wireless-gamepad-quick-start-guide.pdf */
const lessons={
 start:['START','开始 / 接管',Object.values(startInstructions).join(' '),'start'],
 dpad_up:['十字键 ↑','↑ 结束并保存本条','示教和策略HIL统一：收尾由操作者完成，松开 RB，等夹爪操作完成后单击↑保存；不要求先回位或等待固定时长。保存完成后可整理场景，再按 START 开新条。左右键不选臂，下键无录制功能；自由接入不录制。','select'],
 dpad_left:['十字键 ←','无需选臂','左右键不用于选择手臂；START 进入人工后，直接 RB 控制右臂。','select'],
 dpad_right:['十字键 →','无需选臂','START 进入人工后，直接按 RB 控制右臂。左右键不切换手臂，无需先按右键。','select'],
 rb:['RB','按住才控右臂','START 进入人工后直接按住 RB，再操作摇杆或升降键。松 RB 后保持当前位置，仍处于人工模式。'+recoveryInstruction,'forward'],
 left_stick:['左摇杆 + RB','前后 / 左右平移','向前推：机器人 +X；向左推：机器人 +Y。右侧演示先前移，再侧移。','translate'],
 right_stick:['右摇杆 + RB','俯仰 / 偏航','上下控制俯仰，左右控制偏航；与左摇杆平移可同时使用。','rotate'],
 lb:['LB + RB','末端上移','保持 RB，按 LB 使右臂末端向上（+Z）移动。','up'],
 lt:['LT + RB','末端下移','保持 RB，压下 LT 使末端向下（−Z）移动；扳机幅度影响速度。','down'],
 rt:['RT','右夹爪开 / 合','进入人工后，每按下并释放一次 RT，切换一次右夹爪开合；持续按住不会连续切换，无需同时按 RB 或选臂。','grip'],
 a:['A','1× / 2× 速度','单击在两档之间切换。右侧分别以 1× 与 2× 演示同一段动作。','speed'],
 x:['X + RB','末端负向滚转','保持 RB，按 X 控制末端绕 X 轴负向滚转。','roll_left'],
 b:['B + RB','末端正向滚转','保持 RB，按 B 控制末端绕 X 轴正向滚转。','roll_right'],
 y:['Y','可选：右臂回积木 demo-V1',homeInstruction,'home'],
 mode:['MODE / X 模式','先检查手柄模式','背面拨到 X。左圆摇杆应移动 joy.cpl 的 X/Y 十字；若变成视角头盔，按 MODE 切回。','select']
};
function padSVG(){const node=(id,shape,label,x,y)=>`<g class="f710-key" data-lesson="${id}" role="button" tabindex="0" aria-label="${lessons[id][0]}：${lessons[id][1]}">${shape}<text x="${x}" y="${y}">${label}</text></g>`;
 return `<svg viewBox="0 0 620 400" class="f710-pad" aria-label="F710 可交互手柄图，点击按钮查看教学"><defs><linearGradient id="f710-shell" x2="0" y2="1"><stop stop-color="#e6e9eb"/><stop offset="1" stop-color="#b2bac1"/></linearGradient></defs>
 <path class="pad-grip" d="M101 120 Q62 149 40 302 Q33 375 91 355 L192 275 H428 L529 355 Q590 375 580 302 Q558 149 519 120Z"/>
 <path class="pad-shell" d="M110 97 Q161 72 217 113 Q310 134 403 113 Q461 72 512 97 Q554 122 537 213 Q518 267 470 265 L385 272 Q310 302 235 272 L150 265 Q101 267 82 213 Q67 121 110 97Z"/>
 ${node('lt','<rect x="108" y="13" width="79" height="30" rx="10"/>','LT',148,33)}${node('rt','<rect x="433" y="13" width="79" height="30" rx="10"/>','RT',473,33)}
 ${node('lb','<rect x="99" y="50" width="101" height="30" rx="9"/>','LB',150,71)}${node('rb','<rect x="420" y="50" width="101" height="30" rx="9"/>','RB',471,71)}
 <circle cx="150" cy="168" r="48" class="pad-well"/>
 <path d="M137 135 H163 V154 H182 V180 H163 V199 H137 V180 H118 V154 H137Z" class="pad-dpad"/>
 ${node('dpad_up','<rect x="137" y="128" width="26" height="26" rx="5"/>','↑',150,147)}${node('dpad_left','<rect x="115" y="153" width="30" height="29" rx="5"/>','←',130,173)}${node('dpad_right','<rect x="155" y="153" width="30" height="29" rx="5"/>','→',170,173)}
 ${node('y','<circle cx="471" cy="126" r="22" class="key-y"/>','Y',471,133)}${node('a','<circle cx="471" cy="205" r="22" class="key-a"/>','A',471,212)}${node('x','<circle cx="431" cy="166" r="22" class="key-x"/>','X',431,173)}${node('b','<circle cx="511" cy="166" r="22" class="key-b"/>','B',511,173)}
 <circle cx="232" cy="250" r="45" class="pad-well"/>${node('left_stick','<circle cx="232" cy="250" r="33"/>','L',232,257)}
 <circle cx="388" cy="250" r="45" class="pad-well"/>${node('right_stick','<circle cx="388" cy="250" r="33"/>','R',388,257)}
 <circle cx="310" cy="156" r="27" class="pad-well"/><text x="310" y="161" class="pad-brand">F710</text>
 <rect x="238" y="151" width="27" height="17" rx="8" class="pad-idle"/>
 ${node('start','<rect x="349" y="147" width="43" height="26" rx="12"/>','▶',370,166)}<text x="370" y="138" class="pad-label">START</text>
 ${node('mode','<rect x="281" y="207" width="58" height="24" rx="11"/>','MODE',310,223)}
 <text x="310" y="362" class="pad-label">点击按钮，查看右侧动作演示</text></svg>`;}
export function teachingHTML(){return `<details class="card hil-teaching" open><summary><strong>手柄怎么用</strong><span class="section-note">按当前流程 · 点击按钮看示范</span></summary><div class="hil-teaching-grid"><div>${padSVG()}<p class="section-note pad-reference">XInput · 左摇杆为 X/Y <a href="https://www.logitech.com/assets/48300/f710-wireless-gamepad-quick-start-guide.pdf" target="_blank" rel="noopener">罗技官方图解 ↗</a></p></div><div class="hil-lesson"><div class="hil-lesson-copy"><span id="hil-key-label" class="eyebrow">F1 · 右臂教学</span><h3 id="hil-lesson-title">先选按钮，再看动作</h3><p id="hil-lesson-description">按键教学独立于当前会话，三个流程均可查看。</p></div><div class="hil-teaching-stage" id="hil-teaching-model"><span class="section-note">点左侧手柄，载入 F1 模型</span></div><div class="hil-lesson-footer"><span id="hil-demo-label">教学动画 · 不控制实体机器人</span><button class="button small" data-teaching-replay disabled>重播</button></div></div></div></details>`;}
export class HILTeaching{
 constructor(root,{route}={}){this.route=route;this.root=root;this.serial=0;this.disposed=false;this.visible=false;this.frame=0;this.key=null;this.handler=e=>{const g=e.target.closest('[data-lesson]');if(g&&(e.type==='click'||['Enter',' '].includes(e.key))){e.preventDefault();this.select(g.dataset.lesson);}if(e.target.closest('[data-teaching-replay]'))this.select(this.key);};root.addEventListener('click',this.handler);root.addEventListener('keydown',this.handler);this.observer=new IntersectionObserver(es=>{this.visible=es.some(e=>e.isIntersecting);if(!this.active())this.stop();});this.observer.observe(root);this.visibility=()=>{if(!this.active())this.stop();};document.addEventListener('visibilitychange',this.visibility);root.addEventListener('toggle',this.visibility);}
 active(){return !this.disposed&&this.root.isConnected&&this.root.open&&this.visible&&!document.hidden;}
 stop(){cancelAnimationFrame(this.frame);this.frame=0;}
 async select(key){if(!lessons[key]||this.disposed)return;this.key=key;this.stop();const n=++this.serial,lesson=lessons[key];this.root.querySelectorAll('[data-lesson]').forEach(el=>{const on=el.dataset.lesson===key;el.classList.toggle('selected',on);el.setAttribute('aria-pressed',String(on));});this.root.querySelector('#hil-key-label').textContent=lesson[0];this.root.querySelector('#hil-lesson-title').textContent=this.route==='rlt'?({start:'START · 开条 / 人工接管 / 交回 VLA',y:'Y · 回位稳定后保存整条',dpad_up:'↑ · 确认局部成功',dpad_right:'→ · 进入局部 RLT'})[key]||lesson[1]:lesson[1];this.root.querySelector('#hil-lesson-description').textContent=this.route==='rlt'&&['start','y','dpad_up','dpad_right'].includes(key)?startInstructions.rlt:key==='start'?(startInstructions[this.route]||lesson[2]):lesson[2];this.root.querySelector('[data-teaching-replay]').disabled=false;
  try{if(!this.loading)this.loading=this.load();await this.loading;if(this.disposed||n!==this.serial)return;this.started=performance.now();this.lastTick=0;const run=t=>{if(!this.active()||n!==this.serial)return;const elapsed=(t-this.started)/1000;if(t-this.lastTick>=33){this.paint(lesson[3],elapsed);this.lastTick=t;}if(elapsed<6)this.frame=requestAnimationFrame(run);};this.frame=requestAnimationFrame(run);}catch(err){if(!this.disposed)this.root.querySelector('#hil-teaching-model').textContent='教学模型暂不可用：'+err.message;}
 }
 async load(){const [THREE,model,poses]=await Promise.all([import('./vendor/three/three.module.min.js'),import('./model-viewer.js'),fetch('./assets/hil/right-arm-poses.json').then(r=>{if(!r.ok)throw Error('教学姿态加载失败');return r.json();})]);const assets=await model.loadAssets();if(this.disposed)return;this.THREE=THREE;this.poseData=poses;this.setPose=model.setPose;this.model=model.buildRobot(assets.doc,assets.geometry,false,assets.manifest.joint_signs);this.scene=new THREE.Scene();this.scene.background=new THREE.Color('#edf2f6');this.scene.add(this.model.root,new THREE.HemisphereLight(0xffffff,0x8e9daa,3));const light=new THREE.DirectionalLight(0xffffff,3);light.position.set(2,-3,5);this.scene.add(light);
  for(const [name,link] of this.model.links)for(const child of link.children)if(child.isMesh){const right=name.startsWith('arm_R_');child.material.color.setHex(right?0x4c8cb9:0xb2bdc5);child.material.transparent=!right;child.material.opacity=right?1:.11;child.material.depthWrite=right;}
  // The wrist mesh ends at local -Y (y=-0.095m), not at +Z.
  // A separate teaching tool is mounted outside that flange; no URDF is modified.
  const tool=new THREE.Group();tool.position.set(0,-.098,0);this.tool=tool;this.jaws=[];this.toolParts=[];
  const add=(geometry,color,parent)=>{const mesh=new THREE.Mesh(geometry,new THREE.MeshStandardMaterial({color}));parent.add(mesh);this.toolParts.push(mesh);return mesh;};
  const mount=add(new THREE.CylinderGeometry(.035,.035,.018,32),0x42586b,tool);mount.position.y=-.009;
  const palm=add(new THREE.BoxGeometry(.115,.044,.06),0x42586b,tool);palm.position.y=-.035;
  for(const sign of [-1,1]){const jaw=new THREE.Group();jaw.userData.sign=sign;jaw.position.set(sign*.048,-.06,0);tool.add(jaw);this.jaws.push(jaw);const finger=add(new THREE.BoxGeometry(.014,.085,.04),0x1e364b,jaw);finger.position.y=-.035;const pad=add(new THREE.BoxGeometry(.006,.037,.038),0xe5ae4f,jaw);pad.position.set(-sign*.009,-.058,0);}
  this.model.links.get('arm_R_Link7').add(tool);
  this.setPose(this.model,Object.fromEntries(poses.joint_names.map((k,i)=>[k,poses.neutral[i]])));const box=new THREE.Box3().setFromObject(this.model.links.get('arm_R_Link1'));const center=box.getCenter(new THREE.Vector3());this.camera=new THREE.PerspectiveCamera(38,1,.01,20);this.camera.up.set(0,0,1);this.center=center;this.distance=Math.max(.9,box.getSize(new THREE.Vector3()).length()*1.6);this.camera.position.copy(center).add(new THREE.Vector3(1,-1,.4).normalize().multiplyScalar(this.distance));this.camera.lookAt(center);
  this.renderer=new THREE.WebGLRenderer({antialias:true,powerPreference:'low-power'});this.renderer.setPixelRatio(Math.min(devicePixelRatio,1.5));this.renderer.outputColorSpace=THREE.SRGBColorSpace;this.renderer.domElement.setAttribute('aria-label','F1 真实 URDF 右臂教学动画，非实时姿态');const host=this.root.querySelector('#hil-teaching-model');host.replaceChildren(this.renderer.domElement);this.resize=new ResizeObserver(()=>{const w=host.clientWidth,h=host.clientHeight;this.renderer.setSize(w,h);this.camera.aspect=w/h;this.camera.updateProjectionMatrix();this.renderer.render(this.scene,this.camera);});this.resize.observe(host);this.root.dataset.modelReady='true';this.renderer.render(this.scene,this.camera);
 }
 paint(mode,t){const d=this.poseData,pose=[...(mode==='home'?d.home_clearance_start:d.neutral)];let clip=mode;if(mode==='translate')clip=t<3?'forward':'left';if(mode==='rotate')clip=t<3?'pitch':'yaw';if(mode==='speed')clip='forward';let phase=(t%3)/3;if(mode==='speed'&&t>=3)phase=((t-3)%1.5)/1.5;const travel=Math.sin(Math.PI*phase)**2;let q=d.clips[clip];if(q){const index=travel*(q.length-1),i=Math.floor(index),f=index-i;for(let j=0;j<7;j++)pose[7+j]=q[i][j]*(1-f)+q[Math.min(i+1,q.length-1)][j]*f;}if(mode==='home'){const u=Math.max(0,Math.min(t/3,1)),f=10*u**3-15*u**4+6*u**5;for(let j=0;j<7;j++)pose[7+j]=d.home_clearance_start[7+j]*(1-f)+d.home_right[j]*f;this.homeStage='return';}
  this.setPose(this.model,Object.fromEntries(d.joint_names.map((k,i)=>[k,pose[i]])));for(const jaw of this.jaws)jaw.position.x=jaw.userData.sign*(mode==='grip'?.012+.038*(1-travel):.04);this.scene.updateMatrixWorld(true);if(mode==='grip'){const target=this.tool.getWorldPosition(new this.THREE.Vector3());this.camera.position.copy(target).add(new this.THREE.Vector3(1,-1,.65).normalize().multiplyScalar(.55));this.camera.lookAt(target);}else{this.camera.position.copy(this.center).add(new this.THREE.Vector3(1,-1,.4).normalize().multiplyScalar(this.distance));this.camera.lookAt(this.center);}this.root.querySelector('#hil-demo-label').textContent=mode==='home'?'先手动避箱 · Y 直接回 V1 · 教学示意，不控制机器人':mode==='grip'?`夹爪示意 · ${travel>.75?'闭合':travel<.25?'张开':'开合中'} · 非实机`:mode==='speed'?`教学动画 · ${t<3?'1×':'2×'} 速度`:'幅度放大示范 · 非实际速度 · 不控制实体机器人';this.root.dataset.demoFrame=String(Math.round(t*30));this.renderer.render(this.scene,this.camera);
 }
 dispose(){this.disposed=true;this.serial++;this.stop();this.observer.disconnect();this.resize?.disconnect();document.removeEventListener('visibilitychange',this.visibility);this.root.removeEventListener('toggle',this.visibility);this.root.removeEventListener('click',this.handler);this.root.removeEventListener('keydown',this.handler);this.model?.materials.forEach(m=>m.dispose());for(const j of this.toolParts||[]){j.geometry.dispose();j.material.dispose();}this.renderer?.dispose();this.renderer?.forceContextLoss();}
}

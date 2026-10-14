/** F1 URDF read-only viewer. All assets are local; this module never sends commands. */
import * as THREE from './vendor/three/three.module.min.js';
import {OrbitControls} from './vendor/three/OrbitControls.js';
import {STLLoader} from './vendor/three/STLLoader.js';

export const JOINT_MAP = Object.freeze(Object.fromEntries([
  ...['l', 'r'].flatMap(side => Array.from({length: 7}, (_, i) =>
    [`arm_${side}_j${i + 1}`, `arm_${side.toUpperCase()}_joint${i + 1}`])),
  ...Array.from({length: 3}, (_, i) => [`head_j${i + 1}`, `head_joint${i + 1}`]),
  ...Array.from({length: 5}, (_, i) => [`body_j${i + 1}`, `body_joint${5 - i}`]),
]));
const MODEL_BASE = new URL('./models/f1_wheel/', import.meta.url);
const radians = degrees => degrees * Math.PI / 180;
const finite = value => typeof value === 'number' && Number.isFinite(value);
const SIGNED_JOINTS = Object.freeze(['arm_l_j7', 'arm_r_j5', 'arm_r_j6']);
let assetsPromise;

function checkedJointSigns(jointSigns) {
  if (!jointSigns || typeof jointSigns !== 'object' || Array.isArray(jointSigns) ||
      Object.keys(jointSigns).length !== SIGNED_JOINTS.length ||
      !SIGNED_JOINTS.every(name => Object.hasOwn(jointSigns, name)) ||
      Object.entries(jointSigns).some(([name, sign]) => !Object.hasOwn(JOINT_MAP, name) || (sign !== 1 && sign !== -1)))
    throw new Error('模型角度映射缺失或无效，请核对发布文件');
  return Object.freeze({...jointSigns});
}

export function measuredValues(joints) {
  const entries = Array.isArray(joints)
    ? joints.map(item => [item.name, item.position ?? item.actual_deg])
    : Object.entries(joints || {}).map(([name, value]) => [name, typeof value === 'object' && value !== null ? value.position : value]);
  return Object.fromEntries(entries.filter(([name, value]) => JOINT_MAP[name] && finite(value)));
}

function vector(text, fallback = [0, 0, 0]) {
  if (!text) return fallback;
  const result = text.trim().split(/\s+/).map(Number);
  if (result.length !== 3 || !result.every(finite)) throw new Error('URDF 三维坐标无效');
  return result;
}

function origin(object, node) {
  object.position.fromArray(vector(node?.getAttribute('xyz')));
  const [r, p, y] = vector(node?.getAttribute('rpy'));
  // URDF fixed-axis roll/pitch/yaw = Rz(yaw) Ry(pitch) Rx(roll).
  object.quaternion.setFromEuler(new THREE.Euler(r, p, y, 'ZYX'));
}

async function fetchChecked(url, mode = 'text') {
  const response = await fetch(url, {credentials: 'same-origin'});
  if (!response.ok) throw new Error(`模型资源读取失败 (${response.status})`);
  return response[mode]();
}

export async function loadAssets() {
  if (!assetsPromise) assetsPromise = (async () => {
    const manifest = await fetchChecked(new URL('manifest.json', MODEL_BASE), 'json');
    if (JSON.stringify(manifest.joint_map) !== JSON.stringify(JOINT_MAP))
      throw new Error('模型关节映射与发布清单不一致');
    if (manifest.joint_input_units !== 'degrees') throw new Error('模型角度单位与反馈不一致');
    manifest.joint_signs = checkedJointSigns(manifest.joint_signs);
    const urdfURL = new URL(manifest.urdf, MODEL_BASE);
    const xml = await fetchChecked(urdfURL);
    const doc = new DOMParser().parseFromString(xml, 'application/xml');
    if (doc.querySelector('parsererror')) throw new Error('URDF 解析失败');
    const meshNames = [...new Set([...doc.querySelectorAll('visual mesh')].map(node => node.getAttribute('filename')))];
    const geometry = new Map(), loader = new STLLoader();
    let index = 0;
    async function worker() {
      while (index < meshNames.length) {
        const name = meshNames[index++], url = new URL(name, urdfURL);
        if (url.origin !== MODEL_BASE.origin || !url.pathname.startsWith(MODEL_BASE.pathname))
          throw new Error('模型资源超出本地资源目录');
        const data = await fetchChecked(url, 'arrayBuffer');
        const mesh = loader.parse(data);
        mesh.computeBoundingBox();
        geometry.set(name, mesh);
      }
    }
    await Promise.all(Array.from({length: 5}, worker));
    return {doc, geometry, manifest};
  })().catch(error => { assetsPromise = null; throw error; });
  return assetsPromise;
}

/** Build the actual URDF hierarchy, with every joint's origin and axis intact. */
export function buildRobot(doc, geometry, ghost = false, jointSigns) {
  const signs = checkedJointSigns(jointSigns);
  const root = new THREE.Group(), links = new Map(), joints = new Map(), children = new Set();
  const materials = [];
  const ghostMaterial = new THREE.MeshBasicMaterial({color: 0xbd735d, transparent: true, opacity: 0.23, depthWrite: false});
  if (ghost) materials.push(ghostMaterial);
  for (const link of doc.documentElement.querySelectorAll(':scope > link')) {
    const name = link.getAttribute('name'), object = new THREE.Group();
    object.name = name;
    links.set(name, object);
    for (const visual of link.querySelectorAll(':scope > visual')) {
      const mesh = visual.querySelector('geometry > mesh');
      if (!mesh || !geometry.has(mesh.getAttribute('filename'))) throw new Error(`缺少模型网格: ${name}`);
      // Neutral studio materials; original geometry and mechanical transforms are unchanged.
      const dark = /wheel.*link2|arm_[LR]_Link[367]|head_Link[12]/.test(name);
      const material = ghost ? ghostMaterial : new THREE.MeshStandardMaterial({
        color: dark ? 0x454440 : 0xbebbb3, roughness: 0.74, metalness: 0.08,
      });
      if (!ghost) materials.push(material);
      const surface = new THREE.Mesh(geometry.get(mesh.getAttribute('filename')), material);
      origin(surface, visual.querySelector(':scope > origin'));
      surface.scale.fromArray(vector(mesh.getAttribute('scale'), [1, 1, 1]));
      object.add(surface);
    }
  }
  for (const node of doc.documentElement.querySelectorAll(':scope > joint')) {
    const parent = node.querySelector('parent')?.getAttribute('link');
    const child = node.querySelector('child')?.getAttribute('link');
    if (!links.has(parent) || !links.has(child) || children.has(child)) throw new Error('URDF 连接树无效');
    const frame = new THREE.Group(), motion = new THREE.Group();
    origin(frame, node.querySelector('origin'));
    frame.add(motion);
    motion.add(links.get(child));
    links.get(parent).add(frame);
    children.add(child);
    const axis = new THREE.Vector3().fromArray(vector(node.querySelector('axis')?.getAttribute('xyz'), [1, 0, 0]));
    if (axis.lengthSq() === 0) throw new Error('URDF 关节轴无效');
    joints.set(node.getAttribute('name'), {object: motion, axis: axis.normalize(), type: node.getAttribute('type')});
  }
  const rootLinks = [...links].filter(([name]) => !children.has(name));
  if (rootLinks.length !== 1) throw new Error('URDF 根节点无效');
  root.add(rootLinks[0][1]);
  for (const name of Object.values(JOINT_MAP)) if (!joints.has(name)) throw new Error(`模型缺少关节: ${name}`);
  return {root, joints, links, materials, jointSigns: signs};
}

export function setPose(model, values) {
  for (const [halName, value] of Object.entries(values)) {
    if (!finite(value) || !JOINT_MAP[halName]) continue;
    const joint = model.joints.get(JOINT_MAP[halName]);
    if (joint?.type === 'revolute' || joint?.type === 'continuous')
      joint.object.quaternion.setFromAxisAngle(joint.axis, radians(value * (model.jointSigns[halName] ?? 1)));
  }
  model.root.updateMatrixWorld(true);
}

export function createRobotViewer(element) {
  if (!(element instanceof HTMLElement)) throw new TypeError('需要模型容器');
  let renderer, controls, actual, target, disposed = false, frame = 0, framed = false;
  let fixedGround=null;
  let last = {}, snapshot = {joints: {}, draft: {}, stale: true}, view = 'full';
  const root = document.createElement('div');
  root.className = 'f1-model-root';
  root.style.cssText = 'position:relative;width:100%;height:100%;min-height:0;overflow:hidden;border-radius:12px;background:#eeece6';
  const canvasHost = document.createElement('div');
  canvasHost.style.cssText = 'position:absolute;inset:0';
  const status = document.createElement('div');
  status.className = 'f1-model-status';
  status.setAttribute('role', 'status');
  status.style.cssText = 'position:absolute;left:18px;right:18px;top:18px;pointer-events:none;font:14px/1.6 system-ui;color:#68645d';
  status.textContent = '正在载入 F1 模型 · 首次约 28 MB';
  const toolbar = document.createElement('div');
  toolbar.style.cssText = 'position:absolute;bottom:18px;left:18px;right:18px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap';
  const hint = document.createElement('span');
  hint.textContent = '拖拽旋转 · 滚轮缩放';
  hint.style.cssText = 'font:13px/1.6 system-ui;color:#78736b';
  const buttons = document.createElement('div');
  buttons.style.cssText = 'display:flex;gap:6px';
  toolbar.append(hint, buttons);
  root.append(canvasHost, status, toolbar);
  element.append(root);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0xeeece6);
  const camera = new THREE.PerspectiveCamera(34, 1, 0.01, 30);
  camera.up.set(0, 0, 1);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x9b9387, 2.5));
  const key = new THREE.DirectionalLight(0xffffff, 3.3);
  key.position.set(3, 1, 4);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 1.3);
  fill.position.set(-2, -2, 2);
  scene.add(fill);
  const grid = new THREE.GridHelper(4, 20, 0xc7c0b5, 0xdcd7cd);
  grid.rotation.x = Math.PI / 2;
  grid.material.transparent = true;
  grid.material.opacity = 0.5;
  grid.position.z = -0.31;
  scene.add(grid);

  function render() {
    frame = 0;
    if (disposed || !renderer || !element.isConnected) return;
    if (root.clientWidth && root.clientHeight) renderer.render(scene, camera);
  }
  function invalidate() { if (!disposed && !frame) frame = requestAnimationFrame(render); }
  function resize() {
    if (!renderer || disposed) return;
    const width = Math.max(1, root.clientWidth), height = Math.max(1, root.clientHeight);
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    if (framed) frameModel();
    invalidate();
  }
  function frameModel(mode = view) {
    if (!actual || !actual.root.visible || !controls) return;
    view = mode;
    const box = new THREE.Box3().setFromObject(actual.root);
    const center = box.getCenter(new THREE.Vector3()), size = box.getSize(new THREE.Vector3());
    if(fixedGround!==null)grid.position.z=fixedGround;
    if (mode === 'upper') center.z = box.max.z - size.z * 0.34;
    const visibleHeight = size.z * (mode === 'upper' ? 0.70 : 1.18);
    const vertical=THREE.MathUtils.degToRad(camera.fov/2),horizontal=Math.atan(Math.tan(vertical)*camera.aspect);
    const direction=new THREE.Vector3(1,-.68,.35).normalize(),right=new THREE.Vector3().crossVectors(camera.up,direction).normalize(),up=new THREE.Vector3().crossVectors(direction,right).normalize();
    let fit=0;for(const x of [box.min.x,box.max.x])for(const y of [box.min.y,box.max.y])for(const z of [box.min.z,box.max.z]){const v=new THREE.Vector3(x,y,z).sub(center);fit=Math.max(fit,v.dot(direction)+Math.max(Math.abs(v.dot(right))/Math.tan(horizontal),Math.abs(v.dot(up))/Math.tan(vertical))*1.12);}
    const distance=mode==='full'?fit:visibleHeight/(2*Math.tan(vertical));
    controls.target.copy(center);
    camera.position.copy(center).add(new THREE.Vector3(1, -0.68, 0.35).normalize().multiplyScalar(distance));
    controls.update();
    invalidate();
  }
  for (const [mode, title] of [['full', '全身'], ['upper', '上肢']]) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = title;
    button.className = 'button small';
    button.style.cssText = 'font:14px system-ui;min-height:34px;padding:6px 12px;background:#fffdf8;border:1px solid #d7d2c7;border-radius:7px;color:#34332e;cursor:pointer';
    button.addEventListener('click', () => frameModel(mode));
    buttons.append(button);
  }
  const observer = new ResizeObserver(resize);
  observer.observe(root);
  try {
    renderer = new THREE.WebGLRenderer({antialias: true, alpha: false, powerPreference: 'low-power'});
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.domElement.style.cssText = 'width:100%;height:100%;display:block;touch-action:none';
    renderer.domElement.setAttribute('aria-label', 'F1 URDF 三维姿态；深色实体为实测，棕色半透明为编辑目标');
    renderer.domElement.setAttribute('role', 'img');
    canvasHost.append(renderer.domElement);
    controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = false;
    controls.minDistance = 0.45;
    controls.maxDistance = 8;
    controls.maxPolarAngle = Math.PI * 0.91;
    controls.addEventListener('change', invalidate);
    resize();
  } catch (error) {
    status.textContent = '此浏览器无法启动 3D 渲染；关节实时数值仍可查看。';
    status.dataset.state = 'error';
    console.warn('F1 model WebGL:', error.message);
  }

  function applySnapshot() {
    if (!actual || disposed) return;
    const measured = measuredValues(snapshot.joints), count = Object.keys(measured).length;
    Object.assign(last, measured);
    const complete = Object.keys(last).length === Object.keys(JOINT_MAP).length;
    actual.root.visible = complete;
    target.root.visible = false;
    if (!complete) {
      status.textContent = `等待关节反馈 ${count}/22 · 尚不显示实测姿态`;
      status.dataset.state = 'waiting';
      invalidate();
      return;
    }
    setPose(actual, last);
    if(fixedGround===null){const wheels=[...actual.links].filter(([n])=>n.startsWith('chassis_')&&n.endsWith('link2'));fixedGround=Math.min(...wheels.map(([,link])=>new THREE.Box3().setFromObject(link).min.z));grid.position.z=fixedGround;}
    const draft = measuredValues(snapshot.draft);
    const differs = Object.entries(draft).some(([name, value]) => Math.abs(last[name] - value) > 0.05);
    const stale = snapshot.stale || count !== 22 || Object.values(snapshot.joints || {}).some(value =>
      value && typeof value === 'object' && (!finite(value.age_s) || value.age_s > 0.5));
    if (differs) {
      setPose(target, {...last, ...draft});
      target.root.visible = true;
      // Draw only moved surfaces; coincident ghosts would tint the live robot.
      for (const [name, link] of target.links) {
        const reference = actual.links.get(name).matrixWorld.elements;
        const changed = link.matrixWorld.elements.some((value, i) => Math.abs(value - reference[i]) > 1e-6);
        for (const child of link.children) if (child.isMesh) child.visible = changed;
      }
    }
    let unsafe=false;
    if(differs&&fixedGround!==null){
      for(const [name,link] of target.links){if(!/^(body_Link|head_Link)/.test(name))continue;for(const child of link.children){if(child.isMesh&&new THREE.Box3().setFromObject(child).min.z<fixedGround-.005)unsafe=true;}}
    }
    for(const material of target.materials)material.color.setHex(unsafe?0xc74432:0xbd735d);
    element.dataset.groundUnsafe=String(unsafe);
    const warning=document.querySelector('#model-ground-warning');if(warning){warning.hidden=!unsafe;warning.textContent='目标躯干模型穿地：这是不可执行的编辑目标。请调整关节组合，地面检查不等于完整碰撞规划。';}
    status.dataset.state = stale ? 'stale' : 'live';
    status.textContent = stale ? '反馈过期 · 显示最近姿态' : differs ? '实测与目标' : '实时姿态';
    status.style.whiteSpace = 'pre-line';
    for (const material of actual.materials) {
      material.transparent = !!stale;
      material.opacity = stale ? 0.50 : 1;
    }
    if (!framed) { frameModel(); framed = true; }
    invalidate();
  }
  if (renderer) loadAssets().then(assets => {
    if (disposed) return;
    actual = buildRobot(assets.doc, assets.geometry, false, assets.manifest.joint_signs);
    target = buildRobot(assets.doc, assets.geometry, true, assets.manifest.joint_signs);
    actual.root.visible = target.root.visible = false;
    scene.add(actual.root, target.root);
    applySnapshot();
  }).catch(error => {
    if (disposed) return;
    status.textContent = `3D 模型未就绪：${error.message}`;
    status.dataset.state = 'error';
    console.warn('F1 model:', error);
  });

  return {
    update(next = {}) {
      snapshot = {joints: next.joints || {}, draft: next.draft || {}, stale: !!next.stale};
      applySnapshot();
    },
    resize,
    dispose() {
      disposed = true;
      if (frame) cancelAnimationFrame(frame);
      observer.disconnect();
      controls?.dispose();
      actual?.materials.forEach(material => material.dispose());
      target?.materials.forEach(material => material.dispose());
      grid.geometry.dispose();
      grid.material.dispose();
      renderer?.dispose();
      renderer?.forceContextLoss();
      root.remove();
    },
  };
}

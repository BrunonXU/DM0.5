/* Local-only UI verification. Uses an isolated temporary workspace, never real project docs. */
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const {spawn, spawnSync} = require('child_process');
const assert = require('assert');

const root = path.resolve(__dirname, '..');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'projects-hub-browser-'));
const workspace = path.join(temp, 'workspace');
fs.mkdirSync(path.join(workspace, 'sample'), {recursive: true});
fs.mkdirSync(path.join(workspace, 'blocks'), {recursive: true});
fs.writeFileSync(path.join(workspace, 'blocks', 'STATUS.md'), '# 积木验收\n2026-10-12：完成测试夹具的任务拆分。\n');
const source = path.join(workspace, 'sample', 'STATUS.md');
fs.writeFileSync(source, '---\nstatus: active\nupdated: 2026-10-12\n---\n# 验收项目\n\n## 当前状态\n浏览器验收专用临时文档。\n2026-10-12：测试夹具确认分拣基线。\n2026-10-13：测试夹具补充日志字段。\n\n## 下一步\n- [ ] 验证来源任务回写\n');
const registry = path.join(temp, 'projects.json');
fs.writeFileSync(registry, JSON.stringify({schema_version: 1, projects: [{
  id: 'sample', name: '验收项目', description: '浏览器测试临时项目', kind: 'delivery', platform: 'F1',
  status: '待核对', stage: '验证', priority: 'P2', tags: ['临时测试'],
  source_paths: ['sample/STATUS.md'], watch_paths: ['sample'], warnings: [],
  milestones: [{id: 'sample-m1', title: '验证工作台', status: 'planned', source_path: 'sample/STATUS.md'}]
}, {id: 'block-sort', name: 'F1 数采支撑', kind: 'tool', platform: 'F1', supports: ['sample'], status: '待关联文档', stage: '未排期', source_paths: ['blocks/STATUS.md'], watch_paths: ['blocks'], milestones: []}, {id: 'f1-console', name: '4242 工作台', kind: 'tool', status: '待核对', stage: '验证', source_paths: [], watch_paths: [], milestones: []}]}));

// All review publication and fake runner files live under this isolated fixture.
const runtime = path.join(temp, 'runtime');
const fakeCodex = path.join(temp, 'fake-codex');
fs.writeFileSync(fakeCodex, `#!/usr/bin/env python3
import json, pathlib, sys, time
sys.stdin.read()
time.sleep(0.8)
if pathlib.Path(__file__).with_name('fail-review').exists():
    raise SystemExit(9)
output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])
packet = json.loads((output.parent / 'packet.json').read_text())
label = '测试周报更新' if packet['mode'] == 'weekly' else '测试日报更新'
previous = packet.get('previous') or {}
projects = previous.get('projects', [])
events = previous.get('events', [])
def strip_hashes(value):
    if isinstance(value, dict):
        value.pop('sha256', None)
        for child in value.values(): strip_hashes(child)
    elif isinstance(value, list):
        for child in value: strip_hashes(child)
strip_hashes(projects)
strip_hashes(events)
candidate = {'headline': label, 'projects': projects, 'events': events, 'report': {'headline': label, 'changes': ['测试夹具：确认本轮变更'], 'next': ['测试夹具：验证下一目标'], 'risks': []}}
output.write_text(json.dumps(candidate, ensure_ascii=False))
`, {mode: 0o755});
const seedScript = path.join(temp, 'seed-review.py');
fs.writeFileSync(seedScript, `import json, sys
sys.path.insert(0, sys.argv[1])
from hub import Hub
from review_store import ReviewStore
hub = Hub(workspace=sys.argv[2], runtime=sys.argv[3], config_path=sys.argv[4])
store = ReviewStore(hub)
hub.update_tracking('sample', {'tier': 'main', 'activity': 'current'})
hub.update_tracking('block-sort', {'activity': 'current'})
packet = store.prepare('baseline')
ev = [{'path': 'sample/STATUS.md', 'line': 9}]
bev = [{'path': 'blocks/STATUS.md', 'line': 2}]
projects = [{'id': 'sample', 'headline': '测试夹具：基线已确认', 'goal': '测试夹具：验证分拣流程', 'goal_status': 'proposed', 'next_step': '测试夹具：扩充评测场景', 'blocker': '', 'evidence': ev}, {'id': 'block-sort', 'headline': '测试夹具：任务已拆分', 'goal': '测试夹具：整理积木路线', 'goal_status': 'proposed', 'next_step': '测试夹具：补齐数据', 'blocker': '', 'evidence': bev}]
projects[0]['stages'] = [{'id':'scene','label':'场景','status':'verified','note':'测试已验证','evidence':[{'path':'sample/STATUS.md','line':9}]},{'id':'train','label':'训练','status':'unknown','note':'缺验收记录','evidence':[]}]
projects[0]['blocker']='测试：待现场验收'
events = [{'id': 'qa-major', 'project_id': 'sample', 'date': '2026-10-12', 'title': '测试：分拣基线已确认', 'detail': '仅用于浏览器验收的临时记录。', 'kind': 'progress', 'importance': 'major', 'evidence': ev}, {'id': 'qa-minor', 'project_id': 'sample', 'date': '2026-10-13', 'title': '测试：日志字段已补充', 'detail': '仅进入详细线时显示。', 'kind': 'maintenance', 'importance': 'minor', 'evidence': [{'path': 'sample/STATUS.md', 'line': 10}]}, {'id': 'qa-blocks', 'project_id': 'block-sort', 'date': '2026-10-12', 'title': '测试：积木任务已拆分', 'detail': '两项目同日事件必须对齐。', 'kind': 'decision', 'importance': 'major', 'evidence': bev}, {'id': 'qa-undated', 'project_id': 'sample', 'date': None, 'title': '测试：候选目标日期未知', 'detail': '未知日期不能冒充发布日期。', 'kind': 'plan', 'importance': 'major', 'evidence': []}]
store.publish({'headline': '测试基线快照', 'projects': projects, 'events': events, 'report': {'headline': '测试基线快照', 'changes': ['测试夹具：建立首次状态'], 'next': ['测试夹具：完成交互验收'], 'risks': []}}, packet['id'])
`);
const seeded = spawnSync('python3', [seedScript, root, workspace, runtime, registry], {cwd: root, encoding: 'utf8'});
if (seeded.status !== 0) throw new Error('Could not seed isolated review: ' + seeded.stderr);

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let server, chrome;
let sequence = 0;
let buffer = '';
const pending = new Map();
const errors = [];
let session;
let bridgeBase;
function send(method, params, sessionId) {
  return new Promise((resolve, reject) => {
    const id = ++sequence;
    const timeout = setTimeout(() => { pending.delete(id); reject(new Error('CDP timeout: ' + method)); }, 15000);
    pending.set(id, {resolve, reject, timeout});
    const command = {id, method, params: params || {}};
    if (sessionId) command.sessionId = sessionId;
    chrome.stdio[3].write(JSON.stringify(command) + '\0');
  });
}
function onData(chunk) {
  buffer += chunk.toString();
  let end;
  while ((end = buffer.indexOf('\0')) >= 0) {
    const raw = buffer.slice(0, end); buffer = buffer.slice(end + 1);
    if (!raw) continue;
    const message = JSON.parse(raw);
    if (message.id && pending.has(message.id)) {
      const request = pending.get(message.id); pending.delete(message.id); clearTimeout(request.timeout);
      if (message.error) request.reject(new Error(JSON.stringify(message.error))); else request.resolve(message.result);
    } else if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails);
    else if (message.method === 'Runtime.bindingCalled' && message.params.name === 'hubTestFetch') bridgeFetch(message.params.payload);
  }
}
async function evaluate(expression) {
  const result = await send('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true}, session);
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
// This environment stalls Chrome's native network stack before a socket is opened.
// Render the real assets in Chrome and transport fetch through Node to the real
// isolated backend. This tests UI/backend behavior, not Chrome-native networking.
async function bridgeFetch(payload) {
  let request;
  try {
    request = JSON.parse(payload);
    const target = new URL(request.url, bridgeBase);
    if (target.origin !== bridgeBase) throw new Error('Blocked non-test-origin request');
    const response = await new Promise((resolve, reject) => {
      const headers = Object.assign({}, request.options.headers || {});
      if (!Object.keys(headers).some(name => name.toLowerCase() === 'origin')) headers.Origin = bridgeBase;
      if (request.options.body !== undefined) headers['Content-Length'] = Buffer.byteLength(request.options.body);
      const transport = http.request(target, {method: request.options.method || 'GET', headers}, res => {
        const chunks = [];
        res.on('data', chunk => chunks.push(chunk));
        res.on('end', () => resolve({status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString('utf8')}));
      });
      transport.on('error', reject);
      transport.setTimeout(10000, () => transport.destroy(new Error('Local test backend timeout')));
      if (request.options.body) transport.write(request.options.body);
      transport.end();
    });
    await evaluate('window.__hubTestFetchResolve(' + JSON.stringify(request.id) + ',' + JSON.stringify(response) + ',null)');
  } catch (error) {
    if (request) { try { await evaluate('window.__hubTestFetchResolve(' + JSON.stringify(request.id) + ',null,' + JSON.stringify(error.message) + ')'); } catch (_) {} }
  }
}
async function reloadApp() {
  await send('Page.reload', {}, session);
  await delay(150);
  const html = (await get(bridgeBase + '/')).data;
  const css = (await get(bridgeBase + '/styles.css')).data;
  const script = (await get(bridgeBase + '/app.js')).data;
  const tree = await send('Page.getFrameTree', {}, session);
  const content = html.replace(/<link\b[^>]*href="\/styles\.css"[^>]*>/, '<style>' + css + '</style>').replace(/<script\b[^>]*src="\/app\.js"[^>]*><\/script>/, '');
  await send('Page.setDocumentContent', {frameId: tree.frameTree.frame.id, html: content}, session);
  await send('Runtime.addBinding', {name: 'hubTestFetch'}, session);
  await evaluate('(function(){var next=0,pending=new Map();window.__hubTestFetchResolve=function(id,data,error){var p=pending.get(id);if(!p)return;pending.delete(id);if(error)p.reject(new Error(error));else p.resolve(new Response(data.body,{status:data.status,headers:data.headers}));};window.fetch=function(url,options){return new Promise(function(resolve,reject){var id=++next;pending.set(id,{resolve:resolve,reject:reject});window.hubTestFetch(JSON.stringify({id:id,url:String(url),options:options||{}}));});};})()');
  await evaluate(script);
}
function get(url) {
  return new Promise((resolve, reject) => {
    http.get(url, res => {
      let data = ''; res.on('data', b => data += b); res.on('end', () => resolve({status: res.statusCode, data}));
    }).on('error', reject);
  });
}
async function until(fn, label) {
  for (let i = 0; i < 180; i++) { if (await fn()) return; await delay(100); }
  throw new Error('Wait failed: ' + label);
}
async function click(selector) {
  return evaluate('(function(){var el=document.querySelector(' + JSON.stringify(selector) + ');if(!el)throw Error("Missing element");el.dispatchEvent(new MouseEvent("click",{bubbles:true}));return true;})()');
}
async function fill(values) {
  return evaluate('(function(){var values=' + JSON.stringify(values) + ';Object.keys(values).forEach(function(id){var el=document.getElementById(id);if(!el)throw Error("Missing input "+id);if(el.type==="checkbox")el.checked=values[id];else el.value=values[id];el.dispatchEvent(new Event("input",{bubbles:true}));el.dispatchEvent(new Event("change",{bubbles:true}));});return true;})()');
}

(async function () {
  const base = 'http://127.0.0.1:19434';
  bridgeBase = base;
  const serverLog = fs.openSync(path.join(temp, 'server.log'), 'w');
  server = spawn('python3', [path.join(root, 'server.py'), '--workspace', workspace, '--runtime', path.join(temp, 'runtime'), '--config', registry, '--host', '127.0.0.1', '--port', '19434', '--interval', '2'], {cwd: root, env: Object.assign({}, process.env, {WHEREWEARE_CODEX: fakeCodex}), stdio: ['ignore', serverLog, serverLog]});
  await until(async () => { try { return (await get(base + '/api/state')).status === 200; } catch (_) { return false; } }, 'server readiness');
  const browserLog = fs.openSync(path.join(temp, 'chrome.log'), 'w');
  chrome = spawn(process.env.CHROME_BIN || 'google-chrome', ['--headless=new', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--no-first-run', '--no-proxy-server', '--disable-background-networking', '--remote-debugging-pipe', '--user-data-dir=' + path.join(temp, 'chrome-profile'), 'about:blank'], {stdio: ['ignore', browserLog, browserLog, 'pipe', 'pipe']});
  chrome.stdio[4].on('data', onData);
  const target = await send('Target.createTarget', {url: 'about:blank'});
  const attached = await send('Target.attachToTarget', {targetId: target.targetId, flatten: true});
  session = attached.sessionId;
  await send('Page.enable', {}, session);
  await send('Runtime.enable', {}, session);
  await send('Emulation.setDeviceMetricsOverride', {width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false}, session);
  await reloadApp();
  await until(async () => evaluate('document.getElementById("timeline").dataset.project === "sample"'), 'current mainline');
  const getState = async () => JSON.parse((await get(base + '/api/state')).data);
  const closeDetail = async () => { if (await evaluate('document.getElementById("detail-dialog").open')) await click('#detail-dialog [data-action="close"]'); };
  const nodeSelector = id => '[data-action="node"][data-id="n:' + id + '"]';
  const eventSelector = id => '[data-action="node"][data-id="e:' + id + '"]';
  const eventX = async id => evaluate('Number(document.querySelector(' + JSON.stringify(eventSelector(id)) + ').querySelector(".node-ring").getAttribute("cx"))');
  const camera = () => evaluate('({start:document.getElementById("timeline").dataset.start,span:document.getElementById("timeline").dataset.span,range:document.getElementById("timeline").dataset.range})');
  const initialCamera = await camera();
  assert.strictEqual(await evaluate('document.querySelectorAll(".stage-item").length'),2);
  assert(await evaluate('!!document.querySelector("#timeline .checkpoint") && !!document.querySelector("#timeline .planned")'),'Blocker and next step are on the lane');
  assert.strictEqual(await evaluate('document.querySelector(".checkpoint").querySelector("[data-date]")'),null,'Undated current state has no fabricated date');
  assert.strictEqual(await evaluate('document.querySelectorAll("#timeline [data-project-line]").length'), 2, 'Mainline and current supporting lane shown together');
  assert.strictEqual(await evaluate('!!document.querySelector("#legend,.main-aura,.stats-grid,#add-node")'), false, 'No rainbow navigation, permanent glow or root add buttons');
  assert.strictEqual(await evaluate('getComputedStyle(document.querySelector(".node-title")).fontSize'), '18px');
  assert.strictEqual(await evaluate('getComputedStyle(document.querySelector("h1")).fontSize'), '32px');
  assert.strictEqual(await eventX('qa-major'), await eventX('qa-blocks'), 'Same date aligns even across supporting projects');
  assert.strictEqual(await evaluate('document.querySelectorAll(".explicit-branch").length'),0,'Ordinary updates do not branch');
  await evaluate('window.__stable=document.querySelector(".node")'); await delay(2800);
  assert(await evaluate('window.__stable===document.querySelector(".node")'),'Clock-only polling keeps DOM stable');
  const previousConfig = JSON.parse(fs.readFileSync(registry));
  for(let i=0;i<20;i++) previousConfig.projects.push({id:'old-'+i,name:'旧项目 '+i,kind:'delivery',source_paths:[],watch_paths:[],evidence_date:'2020-01-01'});
  fs.writeFileSync(registry,JSON.stringify(previousConfig));
  await until(async()=> (await getState()).projects.length === 23,'20 old projects registered');
  await delay(2700);
  await click('#pending-update');
  assert.deepStrictEqual(await camera(),initialCamera,'Old projects do not change current date domain');
  assert.strictEqual(await evaluate('document.querySelectorAll("#timeline [data-project-line]").length'),2);
  assert.strictEqual(await evaluate('getComputedStyle(document.querySelector(".node-title")).fontSize'),'18px');
  await click(eventSelector('qa-major'));
  await evaluate('document.querySelector("#detail-dialog .evidence").open=true');
  await click('#detail-dialog [data-action="source"]');
  await until(async()=>evaluate('document.getElementById("source-content").textContent.includes("浏览器验收专用临时文档")'),'Evidence source');
  await closeDetail();
  await click('#project-heading');
  assert.strictEqual(await evaluate('document.getElementById("timeline").dataset.focus'),'sample');
  assert.deepStrictEqual(await camera(),initialCamera,'Click expand retains date anchor');
  assert(await evaluate('!!document.querySelector('+JSON.stringify(eventSelector('qa-minor'))+')'),'Minor changes only in detail');
  assert.strictEqual(await eventX('qa-major'),await eventX('qa-blocks'),'Expanded supporting track preserves dates');
  await click('[data-action="node"][data-id="records:sample"]');
  await click('[data-action="node"][data-id="e:qa-undated"]');
  assert(await evaluate('document.getElementById("detail-dialog").textContent.includes("未排期")'),'Undated fact has no invented date');
  await closeDetail(); await click('#back-global');
  assert.deepStrictEqual(await camera(),initialCamera,'Back restores camera');
  // Ordinary wheel is native page scroll, never time zoom or automatic drill-in.
  const wheelBefore=await camera();
  const wheelResult=await evaluate('(function(){var e=new WheelEvent("wheel",{deltaY:100,bubbles:true,cancelable:true});document.getElementById("plot-viewport").dispatchEvent(e);return e.defaultPrevented;})()');
  assert.strictEqual(wheelResult,false,'Vertical wheel is not intercepted');
  assert.deepStrictEqual(await camera(),wheelBefore);
  assert.strictEqual(await evaluate('document.getElementById("timeline").dataset.focus'),'');
  assert.strictEqual(await evaluate('!!document.querySelector("#zoom-label,[data-action=zoom-in]")'),false);
  const beforeNow=await evaluate('document.getElementById("timeline").dataset.currentX');
  await evaluate('document.getElementById("plot-viewport").dispatchEvent(new WheelEvent("wheel",{deltaX:130,bubbles:true,cancelable:true}))');
  assert.notDeepStrictEqual(await camera(),wheelBefore,'Horizontal wheel pans dates');
  assert.strictEqual(await evaluate('document.getElementById("timeline").dataset.currentX'),beforeNow,'Current state stays pinned');
  assert(await evaluate('!document.getElementById("return-now").hidden'));
  await click('[data-action="today"]');assert.deepStrictEqual(await camera(),wheelBefore);
  await click('[data-action="range"][data-range="month"]');
  assert.strictEqual((await camera()).span,'45');
  await click('[data-action="range"][data-range="all"]');
  assert.strictEqual((await camera()).range,'all');
  await click('[data-action="range"][data-range="fortnight"]');
  // Real Chromium wheel input must scroll the document while leaving time unchanged.
  await evaluate('document.body.style.minHeight="1800px"');
  const wheelPoint=await evaluate('({x:document.getElementById("plot-viewport").getBoundingClientRect().left+70,y:document.getElementById("plot-viewport").getBoundingClientRect().top+100})');
  await send('Input.dispatchMouseEvent',{type:'mouseWheel',x:wheelPoint.x,y:wheelPoint.y,deltaY:180,deltaX:0},session);
  await delay(250);
  assert(await evaluate('scrollY>0'),'Native wheel scrolls the page');
  assert.deepStrictEqual(await camera(),wheelBefore);
  await evaluate('document.body.style.minHeight="";window.scrollTo(0,0)');
  await click('#project-menu');
  await click('[data-action="activity"][data-value="paused"]');
  await until(async()=> (await getState()).projects.find(p=>p.id==='sample').tracking.activity==='paused','Activity saved');
  await until(async()=>evaluate("document.querySelector('[data-action=\"activity\"][data-value=\"paused\"]').getAttribute(\"aria-pressed\") === \"true\""),"Activity UI reflected");
  await click('[data-action="activity"][data-value="current"]');
  await until(async()=> (await getState()).projects.find(p=>p.id==='sample').tracking.activity==='current','Activity restored');
  await until(async()=>evaluate("document.querySelector('[data-action=\"activity\"][data-value=\"current\"]').getAttribute(\"aria-pressed\") === \"true\""),"Activity UI reflected");
  await click('#archive-project'); await until(async()=> (await getState()).projects.find(p=>p.id==='sample').tracking.archived,'Archive saved');
  await until(async()=>evaluate('document.getElementById("archive-project").dataset.archived==="false"'),'Archive reflected');
  await click('#archive-project'); await until(async()=> !(await getState()).projects.find(p=>p.id==='sample').tracking.archived,'Restore saved');
  await until(async()=>evaluate('document.getElementById("archive-project").dataset.archived==="true"'),'Restore reflected');
  await closeDetail(); await click('[data-action="library"]');
  assert(await evaluate('document.querySelectorAll(".library-item").length===23'),'All projects remain in library');
  await click('.library-item[data-id="block-sort"]');
  assert.strictEqual(await evaluate('document.getElementById("timeline").dataset.project'),'block-sort');
  await click('#back-global');
  assert.strictEqual(await evaluate('document.getElementById("timeline").dataset.project'),'sample');
  const originalSource=fs.readFileSync(source,'utf8');
  async function save(values){await fill(values);await click('#form-submit');await until(async()=>evaluate('!document.getElementById("form-dialog").open'),'Node saved');}
  await click('#project-heading');await click('#project-menu');await click('[data-action="new"][data-id="sample"]');
  await save({'node-title':'分拣基线','node-date':'2026-10-12','node-status':'active','node-notes':'说明 <img src=x onerror=window.qaInjected=true>'});
  const parent=(await getState()).timeline_nodes.find(n=>n.title==='分拣基线');
  await click(nodeSelector(parent.id));assert.strictEqual(await evaluate('!!window.qaInjected'),false,'Escaped notes');
  await click('[data-action="branch"][data-id="'+parent.id+'"]');
  await save({'node-title':'优化抓取','node-date':'2026-10-14','node-status':'active'});
  const child=(await getState()).timeline_nodes.find(n=>n.title==='优化抓取');
  assert.strictEqual(child.parent_id,parent.id);
  assert(await evaluate('document.querySelectorAll(".explicit-branch").length===1'),'Explicit branch drawn');
  await click(nodeSelector(child.id));await click('[data-action="edit"][data-id="'+child.id+'"]');
  await save({'node-title':'优化抓取完成','node-status':'done'});
  assert.strictEqual((await getState()).timeline_nodes.find(n=>n.id===child.id).status,'done');
  await click(nodeSelector(child.id));
  await click('[data-action="branch"][data-id="'+child.id+'"]');
  await save({'node-title':'二级实验分支','node-date':'2026-10-15','node-status':'planned'});
  const childY=await evaluate('Number(document.querySelector('+JSON.stringify(nodeSelector(child.id))+').querySelector(".node-ring").getAttribute("cy"))');
  const branches=await evaluate('Array.from(document.querySelectorAll(".explicit-branch")).map(p=>p.getAttribute("d"))');
  assert(branches.some(d=>Number(d.match(/^M[^,]+,([\d.]+)/)[1])===childY),'Nested branch connects to actual parent');
  assert.strictEqual(fs.readFileSync(source,'utf8'),originalSource,'Manual records do not rewrite evidence');
  await click('[data-action="range"][data-range="fortnight"]');
  await click('[data-action="node"][data-id="next:sample"]');
  await click('[data-action="decision"][data-value="paused"]');
  await until(async()=>evaluate('!document.getElementById("detail-dialog").open'),'Decision saved');
  assert.strictEqual(await evaluate(`!!document.querySelector('[data-id="next:sample"]')`),false,'Paused recommendation leaves timeline');
  assert((await getState()).whereweare.decisions.some(d=>d.status==='paused'),'Decision persists');
  await click('[data-action="node"][data-id="records:sample"]');
  await click('[data-action="node"][data-id^="decision:next:"]');
  await click('[data-action="decision"][data-value="active"]');
  await until(async()=>evaluate('!document.getElementById("detail-dialog").open'),'Decision resumed');
  assert(await evaluate(`!!document.querySelector('[data-id="next:sample"]')`),'Resumed recommendation returns');
  const memoryBeforeReports=JSON.stringify((await getState()).whereweare.snapshot);
  await click('#review-menu');await delay(1200);
  assert.strictEqual((await getState()).whereweare.job,null,'Opening review is read-only');
  assert.strictEqual(await evaluate('document.getElementById("report-headline").textContent'),'本期尚未生成');
  await click('[role="tab"][data-mode="weekly"]');await delay(300);
  assert.strictEqual((await getState()).whereweare.job,null,'Switching periods is read-only');
  await click('[role="tab"][data-mode="daily"]');
  await click('#run-review');
  await until(async()=> (await getState()).whereweare.job.status==='succeeded','Explicit daily generation');
  await until(async()=>evaluate('document.getElementById("report-headline").textContent==="测试日报更新"'),'Daily report');
  const dailyJob=(await getState()).whereweare.job.id;
  await click('#run-review');await delay(500);
  assert.strictEqual((await getState()).whereweare.job.id,dailyJob,'Same fact version reuses report');
  await click('[role="tab"][data-mode="weekly"]');
  fs.writeFileSync(path.join(temp,'fail-review'),'failure fixture');
  await click('#run-review');
  await until(async()=> (await getState()).whereweare.job.status==='failed','Report failure');
  await until(async()=>evaluate('!!document.getElementById("review-error")'),'Failure visible');
  await click('[role="tab"][data-mode="daily"]');
  assert.strictEqual(await evaluate('document.getElementById("report-headline").textContent'),'测试日报更新');
  assert.strictEqual(await evaluate('!!document.getElementById("review-error")'),false,'Other period failure does not leak');
  fs.unlinkSync(path.join(temp,'fail-review'));
  await click('[role="tab"][data-mode="weekly"]');await click('#run-review');
  await until(async()=> (await getState()).whereweare.job.status==='succeeded','Explicit weekly generation');
  await until(async()=>evaluate('document.getElementById("report-headline").textContent==="测试周报更新"'),'Weekly report');
  assert.strictEqual(JSON.stringify((await getState()).whereweare.snapshot),memoryBeforeReports,'Reports never change homepage memory');
  await closeDetail();await click('#review-menu');await closeDetail();await delay(500);
  assert.strictEqual(await evaluate('document.getElementById("detail-dialog").open'),false,'No async reopening');
  fs.appendFileSync(source,'\n- [ ] 外部文档新增任务\n');
  await until(async()=> (await getState()).tasks.some(t=>t.title==='外部文档新增任务'),'Source task indexed');
  await click('#project-heading');
  await delay(2800); if(await evaluate('!document.getElementById("pending-update").hidden'))await click('#pending-update'); await click('[data-action="node"][data-id="records:sample"]');
  assert(await evaluate('document.getElementById("detail-dialog").textContent.includes("外部文档新增任务")'),'Undated tasks remain off date axis');await closeDetail();
  await click('[data-action="range"][data-range="fortnight"]');
  await send('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false},session);
  await delay(200);
  await evaluate('document.querySelector("#timeline .node").focus()');
  await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13},session);
  await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13},session);
  assert(await evaluate('document.getElementById("detail-dialog").open'),'Keyboard opens evidence');
  await closeDetail();
  assert(await evaluate('document.activeElement.classList.contains("node")'),'Closing restores node keyboard focus');
  const panBefore=await camera();
  const point=await evaluate('({x:document.getElementById("graphstage").getBoundingClientRect().left+350,y:document.getElementById("graphstage").getBoundingClientRect().top+180})');
  await send('Input.dispatchMouseEvent',{type:'mousePressed',x:point.x,y:point.y,button:'left',clickCount:1},session);
  await send('Input.dispatchMouseEvent',{type:'mouseMoved',x:point.x+100,y:point.y,button:'left',buttons:1},session);
  await send('Input.dispatchMouseEvent',{type:'mouseReleased',x:point.x+100,y:point.y,button:'left'},session);
  assert.notDeepStrictEqual(await camera(),panBefore,'Drag pans time');
  await click('[data-action="range"][data-range="fortnight"]');
  const desktop=await evaluate('({width:innerWidth,scroll:document.documentElement.scrollWidth,title:getComputedStyle(document.querySelector("h1")).fontSize,nextY:document.getElementById("graphstage").getBoundingClientRect().bottom})');
  assert(desktop.scroll<=desktop.width+1);assert(desktop.nextY<=900,'Core content fits first screen');
  const shot=await send('Page.captureScreenshot',{format:'png'},session);fs.writeFileSync(path.join(temp,'desktop.png'),Buffer.from(shot.data,'base64'));
  await send('Emulation.setDeviceMetricsOverride',{width:1920,height:1080,deviceScaleFactor:1,mobile:false},session);await delay(200);
  const wide=await evaluate('({width:innerWidth,scroll:document.documentElement.scrollWidth,bottom:document.getElementById("graphstage").getBoundingClientRect().bottom})');
  assert(wide.scroll<=wide.width+1);assert(wide.bottom<=1080);
  const wideShot=await send('Page.captureScreenshot',{format:'png'},session);fs.writeFileSync(path.join(temp,'desktop-wide.png'),Buffer.from(wideShot.data,'base64'));
  // Hold the visible fact/action pair until the reader accepts a new version.
  const oldActionKey=(await getState()).whereweare.action_keys.sample;
  const snapshotPath=path.join(runtime,'tracking','current.json');
  const nextSnapshot=JSON.parse(fs.readFileSync(snapshotPath));
  nextSnapshot.revision++;
  nextSnapshot.projects.find(p=>p.id==='sample').next_step='测试：审阅新版动作';
  fs.writeFileSync(snapshotPath,JSON.stringify(nextSnapshot));
  await until(async()=>evaluate('!document.getElementById("pending-update").hidden'),'New version offered');
  await click('[data-action="node"][data-id="next:sample"]');
  assert.strictEqual(await evaluate(`document.querySelector('[data-action="decision"]').dataset.key`),oldActionKey,'Visible action retains original identity');
  await closeDetail();
  await click('#pending-update');
  await click('[data-action="node"][data-id="next:sample"]');
  assert(await evaluate('document.getElementById("detail-title").textContent.includes("新版动作")'),'Explicit load applies new action');
  assert.notStrictEqual(await evaluate(`document.querySelector('[data-action="decision"]').dataset.key`),oldActionKey);
  await closeDetail();
  assert.deepStrictEqual(errors,[],'No JavaScript errors');
  fs.writeFileSync(path.join(temp,'result.json'),JSON.stringify({desktop,wide,errors},null,2));
  console.log(JSON.stringify({ok:true,artifacts:temp,desktop,wide,javascriptErrors:errors.length}));
})().catch(async error => {
  console.error(error.stack);
  if (session) { try { console.error(JSON.stringify(await evaluate('({text:document.body&&document.body.innerText,svg:document.querySelector("#timeline")&&document.querySelector("#timeline").outerHTML.slice(0,2500)})'))); } catch (_) {} }
  console.error('Artifacts: ' + temp); process.exitCode = 1;
}).finally(async () => {
  if (chrome) { try { await send('Browser.close'); } catch (_) { chrome.kill('SIGTERM'); } }
  if (server) server.kill('SIGTERM');
});

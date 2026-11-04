/* Real-media, isolated network regression for the three-camera player.
 * Serves only local assets and fake GET APIs on a random loopback port. A large
 * faststart MP4 is transferred in paced chunks; withholding head chunks causes
 * genuine browser waiting/seeking events. No media properties are overwritten.
 *
 * NODE_PATH=/usr/share/nodejs node scripts/check_video_sync_browser.cjs
 * Optional: F1_VIDEO_SYNC_OUTPUT, F1_VIDEO_SYNC_WEB_ROOT, F1_VIDEO_SYNC_FIXTURE,
 *           CHROME_BIN, FFMPEG_BIN, F1_VIDEO_SYNC_CASE=speed-only.
 * Requires H.264 support in Chrome and ffmpeg.
 */
const fs = require('fs'), http = require('http'), os = require('os');
const path = require('path'), cp = require('child_process'), assert = require('assert');
const WebSocket = require('ws');
const root = path.resolve(__dirname, '..');
const output = path.resolve(process.env.F1_VIDEO_SYNC_OUTPUT || path.join(root, 'results/video-sync-20261023/browser'));
const webRoot = path.resolve(process.env.F1_VIDEO_SYNC_WEB_ROOT || path.join(root, 'web'));
const duration = 24, headRate = 1024 * 1024, chunkSize = 16 * 1024;
const episodeId = 'eeeeeeeeeeeeeeeeeeeeeeee';
const cameras = ['left', 'head', 'right'];
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const report = {isolated: true, robotActionsSent: 0, realNetworkThrottle: true,
  durationSeconds: duration, headBytesPerSecond: headRate, chunkBytes: chunkSize,
  fixture: null, checks: [], samples: [], events: [], mediaRequests: [],
  blockedWrites: [], exceptions: [], networkControl: [], requests: [], browserNetwork: [], connections: []};
let headHeld = false, caseName = '', chrome, ws, server, debugCall;
let browserProfile;
const sockets = new Set();

function fixture() {
  fs.mkdirSync(output, {recursive: true});
  const file = path.resolve(process.env.F1_VIDEO_SYNC_FIXTURE || path.join(output, 'sync-fixture.mp4'));
  if (!fs.existsSync(file)) {
    const args = ['-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=30',
      '-t', String(duration), '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
      '-b:v', '6M', '-minrate', '6M', '-maxrate', '6M', '-bufsize', '6M',
      '-x264-params', 'nal-hrd=cbr:force-cfr=1', '-g', '15', '-keyint_min', '15', '-sc_threshold', '0',
      '-movflags', '+faststart', '-an', '-y', file];
    const result = cp.spawnSync(process.env.FFMPEG_BIN || 'ffmpeg', args, {encoding: 'utf8', timeout: 120000});
    if (result.status !== 0) throw Error('Fixture encoding failed: ' + (result.stderr || result.error));
  }
  const size = fs.statSync(file).size;
  assert(size > 10 * 1024 * 1024, 'The fixture must exceed 10 MiB so metadata preload cannot fetch the whole clip.');
  report.fixture = {file, bytes: size};
  return file;
}

function setHeadHeld(value) {
  headHeld = value;
  report.networkControl.push({case: caseName, held: value, wallMs: Date.now()});
}

function serveMedia(req, res, file, camera) {
  const size = fs.statSync(file).size, match = (req.headers.range || '').match(/^bytes=(\d+)-(\d*)$/);
  const start = match ? Number(match[1]) : 0;
  const end = match && match[2] ? Math.min(Number(match[2]), size - 1) : size - 1;
  const request = {case: caseName, camera, range: req.headers.range || null, start, end, bytes: 0,
    startedAtMs: Date.now(), heldWrites: 0};
  report.mediaRequests.push(request);
  if (start >= size || end < start) {res.writeHead(416, {'Content-Range': 'bytes */' + size}); res.end(); return;}
  res.statusCode = match ? 206 : 200;
  res.setHeader('Content-Type', 'video/mp4'); res.setHeader('Accept-Ranges', 'bytes');
  res.setHeader('Cache-Control', 'no-store'); res.setHeader('Content-Length', end - start + 1);
  if (match) res.setHeader('Content-Range', `bytes ${start}-${end}/${size}`);
  if (camera !== 'head') {
    const stream = fs.createReadStream(file, {start, end});
    stream.on('data', chunk => {request.bytes += chunk.length;});
    res.on('close', () => {request.finishedAtMs = Date.now(); stream.destroy();}); stream.pipe(res); return;
  }
  const fd = fs.openSync(file, 'r'); let position = start, timer, closed = false;
  const finish = () => {if (closed) return; closed = true; clearTimeout(timer); fs.closeSync(fd); request.finishedAtMs = Date.now();};
  res.on('close', finish);
  const send = () => {
    if (closed || res.destroyed) return;
    if (headHeld) {request.heldWrites++; timer = setTimeout(send, 25); return;}
    if (position > end) {res.end(); return;}
    const chunk = Buffer.allocUnsafe(Math.min(chunkSize, end - position + 1));
    const count = fs.readSync(fd, chunk, 0, chunk.length, position);
    if (!count) {res.end(); return;}
    position += count; request.bytes += count;
    const schedule = () => {timer = setTimeout(send, count / headRate * 1000);};
    if (res.write(chunk.subarray(0, count))) schedule(); else res.once('drain', schedule);
  };
  send();
}

function fakeServer(file) {
  const profile = JSON.parse(fs.readFileSync(path.join(root, 'configs/f1-09.json')));
  const groups = {left_arm: [], right_arm: [], body: [], head: []};
  const joints = {};
  Object.keys(profile.joint_limits_deg).forEach(name => {
    groups[name.startsWith('arm_l') ? 'left_arm' : name.startsWith('arm_r') ? 'right_arm' : name.startsWith('body') ? 'body' : 'head'].push(name);
    joints[name] = {position: 0, age_s: .01};
  });
  const robot = {id: 'F1-video-sync-test', label: '隔离同步验证', groups,
    joint_limits_deg: profile.joint_limits_deg, capabilities: {}};
  const episode = {id: episodeId, name: 'f1_hil_sync_network_fixture', path: '/mock/recordings/sync',
    source: 'hil_demonstration', source_signature: 'network-fixture-v1', execution: 'powered',
    recording_status: 'committed', duration_s: duration, size_bytes: fs.statSync(file).size * 3,
    started_at: '2026-10-23T00:00:00Z', review: {revision: 'none', status: 'pending'}, files: [], topics: [],
    videos: cameras.map(camera => ({camera, url: `/api/data/video/${episodeId}/${camera}.mp4`, offset_s: 0}))};
  return http.createServer(async (req, res) => {
    for await (const unused of req) {} // Drain request bodies, matching the normal isolated API harness.
    const url = new URL(req.url, 'http://127.0.0.1');
    report.requests.push({case: caseName, path: req.url, atMs: Date.now()});
    if (req.method !== 'GET') {report.blockedWrites.push({method: req.method, path: url.pathname}); res.writeHead(405); res.end(); return;}
    if (url.pathname.startsWith('/api/data/video/')) {serveMedia(req, res, file, path.basename(url.pathname, '.mp4')); return;}
    let data;
    if (url.pathname === '/api/robot') data = {robot};
    else if (url.pathname === '/api/session') data = {authenticated: true};
    else if (url.pathname === '/api/state') data = {connected: true, authenticated: true, control: {}, joints, grippers: {}, cameras: {}, services: {}, capabilities: {}, action: null};
    else if (url.pathname === '/api/data') data = {episodes: [episode], available: true, recording: {state: 'no_process_observed'}};
    else if (url.pathname.startsWith('/api/data/episodes/')) data = {episode};
    else if (url.pathname === '/api/replay/review') data = {review: {executable: false, can_align: false, issues: ['隔离测试'], command_count: 0, duration_s: duration}};
    else if (url.pathname === '/api/replay/reports') data = {reports: []};
    else if (url.pathname === '/api/presets') data = {presets: []};
    else if (url.pathname === '/api/events') data = {events: []};
    else if (url.pathname === '/api/dev/revision') data = {revision: 'isolated-sync-test'};
    else if (url.pathname.startsWith('/api/')) data = {};
    if (data !== undefined) {res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(data)); return;}
    const relative = url.pathname === '/' ? 'index.html' : decodeURIComponent(url.pathname).slice(1);
    const asset = path.resolve(webRoot, relative);
    if (!asset.startsWith(webRoot + path.sep) || !fs.existsSync(asset) || !fs.statSync(asset).isFile()) {res.writeHead(404); res.end(); return;}
    res.setHeader('Content-Type', ({'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json'})[path.extname(asset)] || 'application/octet-stream');
    fs.createReadStream(asset).pipe(res);
  });
}

function getJson(url) {
  return new Promise((resolve, reject) => http.get(url, reply => {
    let data = ''; reply.on('data', chunk => {data += chunk;});
    reply.on('end', () => {try {resolve(JSON.parse(data));} catch (error) {reject(error);}});
  }).on('error', reject));
}

async function main() {
  const file = fixture(); server = fakeServer(file);
  server.on('connection', socket => {
    sockets.add(socket); socket.on('close', () => sockets.delete(socket));
    const entry = {remotePort: socket.remotePort, atMs: Date.now(), bytes: 0}; report.connections.push(entry);
    socket.on('data', chunk => {entry.bytes += chunk.length;});
  });
  await new Promise((resolve, reject) => {server.once('error', reject); server.listen(Number(process.env.F1_VIDEO_SYNC_PORT) || 0, '127.0.0.1', resolve);});
  browserProfile = fs.mkdtempSync(path.join(os.tmpdir(), 'f1-sync-browser-'));
  chrome = cp.spawn(process.env.CHROME_BIN || '/usr/bin/google-chrome', ['--headless=new', '--no-sandbox',
    '--no-proxy-server', '--disable-background-networking', '--password-store=basic',
    '--disable-dev-shm-usage', '--enable-unsafe-swiftshader',
    '--remote-debugging-port=0', '--log-net-log=' + path.join(output, 'chrome-netlog.json'),
    '--user-data-dir=' + browserProfile, 'about:blank'], {stdio: 'ignore'});
  const active = path.join(browserProfile, 'DevToolsActivePort');
  for (let index = 0; index < 100 && !fs.existsSync(active); index++) await wait(100);
  assert(fs.existsSync(active), 'Chrome did not start.');
  const debugPort = fs.readFileSync(active, 'utf8').split('\n')[0];
  const tabs = await getJson('http://127.0.0.1:' + debugPort + '/json/list');
  ws = new WebSocket(tabs.find(tab => tab.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {ws.on('open', resolve); ws.on('error', reject);});
  let id = 0; const pending = new Map();
  ws.on('message', raw => {
    const message = JSON.parse(raw);
    if (message.id && pending.has(message.id)) {
      const entry = pending.get(message.id); pending.delete(message.id); clearTimeout(entry.timer);
      if (message.error) entry.reject(Error(JSON.stringify(message.error))); else entry.resolve(message.result);
    } else if (message.method === 'Runtime.exceptionThrown') report.exceptions.push(message.params.exceptionDetails);
    else if (message.method === 'Network.requestWillBeSent') report.browserNetwork.push({url: message.params.request.url, at: message.params.timestamp});
    else if (message.method === 'Network.loadingFailed') report.browserNetwork.push(message.params);
  });
  const call = debugCall = (method, params = {}) => new Promise((resolve, reject) => {
    const n = ++id, timer = setTimeout(() => {pending.delete(n); reject(Error(method + ' timed out'));}, method === 'Page.navigate' ? 45000 : 15000);
    pending.set(n, {resolve, reject, timer}); ws.send(JSON.stringify({id: n, method, params}));
  });
  const evaluate = async expression => {
    const result = await call('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
    if (result.exceptionDetails) throw Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };
  const until = async (expression, label, timeout = 16000) => {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {if (await evaluate(expression)) return; await wait(100);}
    throw Error('Timed out: ' + label);
  };
  const click = selector => evaluate('document.querySelector(' + JSON.stringify(selector) + ').click()');
  const sample = async () => {
    const value = await evaluate(`({browserAtMs:performance.now(),time:Number(document.querySelector('[data-media-time-input]')?.value),
      status:document.querySelector('[data-media-status]')?.textContent,button:document.querySelector('[data-media-play]')?.textContent,
      videos:[...document.querySelectorAll('[data-media-viewer] video')].map(v=>({camera:v.dataset.camera,currentTime:v.currentTime,
        paused:v.paused,seeking:v.seeking,readyState:v.readyState,frame:window.syncFrames?.[v.dataset.camera]||null,
        buffered:[...Array(v.buffered.length)].map((_,i)=>[v.buffered.start(i),v.buffered.end(i)])})),lastWait:window.syncWaitCalls?.at(-1)})`);
    value.case = caseName; value.wallMs = Date.now(); report.samples.push(value); return value;
  };
  const collect = async milliseconds => {
    const result = [], deadline = Date.now() + milliseconds;
    while (Date.now() < deadline) {result.push(await sample()); await wait(80);}
    return result;
  };
  const spread = value => value.videos.every(video => video.frame) ?
    Math.max(...value.videos.map(video => video.frame.mediaTime)) - Math.min(...value.videos.map(video => video.frame.mediaTime)) : Infinity;
  const assertStable = values => {
    assert(values.every(value => value.videos.length === 3 && value.videos.every(video => video.frame)), 'Every stream must present real decoded frames.');
    const maximum = Math.max(...values.map(spread));
    assert(maximum <= .1001, 'Presented frames drifted ' + maximum.toFixed(3) + ' seconds.'); return maximum;
  };
  const open = async name => {
    caseName = name; setHeadHeld(false);
    if (report.checks.length) await evaluate('localStorage.clear();sessionStorage.clear()');
    await call('Page.navigate', {url: `http://127.0.0.1:${server.address().port}/?syncCase=${name}#data?episode=${episodeId}`});
    await until("document.querySelector('#entry-notice')?.open", 'entry notice'); await click('#enter-workbench');
    await until("document.querySelectorAll('[data-media-viewer] video').length===3", 'three local MP4 videos');
    await evaluate(`(async()=>{const {SynchronizedVideos}=await import('/synced-video.js');
      window.syncWaitCalls=[];const original=SynchronizedVideos.prototype.wait;
      SynchronizedVideos.prototype.wait=function(reason){syncWaitCalls.push({atMs:performance.now(),reason,phase:this.phase,rate:this.rate,time:this.time,stack:new Error().stack,
        videos:this.videos.map(v=>({camera:v.dataset.camera,currentTime:v.currentTime,paused:v.paused,readyState:v.readyState,seeking:v.seeking,frame:this.frames.get(v)}))});return original.call(this,reason);};})()`);
    await evaluate(`document.querySelector('[data-media-viewer]').scrollIntoView({block:'start'});
      window.syncFrames={};window.syncEvents=[];
      document.querySelectorAll('[data-media-viewer] video').forEach(v=>{
        const frame=(at,m)=>{syncFrames[v.dataset.camera]={mediaTime:m.mediaTime,presentedAtMs:at,expectedDisplayTimeMs:m.expectedDisplayTime,presentedFrames:m.presentedFrames};v.requestVideoFrameCallback(frame);};
        v.requestVideoFrameCallback(frame);
        for(const type of ['waiting','playing','pause','seeking','seeked','canplay','error'])v.addEventListener(type,()=>
          syncEvents.push({camera:v.dataset.camera,type,atMs:performance.now(),currentTime:v.currentTime}));});`);
    await evaluate(`{const seek=document.querySelector('[data-media-seek]');seek.value='0';seek.dispatchEvent(new Event('input',{bubbles:true}));}`);
    await wait(150);
    await click('[data-media-play]');
    await until("Object.keys(syncFrames).length===3&&Object.values(syncFrames).every(f=>f.mediaTime>1.5)", 'three streams playing actual frames');
    assertStable(await collect(400));
  };
  const outage = async () => {
    const marker = await evaluate('performance.now()'); setHeadHeld(true);
    await until(`syncEvents.some(e=>e.camera==='head'&&e.type==='waiting'&&e.atMs>${marker})`, 'real head network starvation');
    await wait(250);
    const held = await collect(650);
    assert(held.every(value => value.videos.every(video => video.paused)), 'All streams must pause when head runs out of real data.');
    const times = held.map(value => value.time);
    assert(Math.max(...times) - Math.min(...times) <= .05, 'Shared timeline advanced while head was buffering.');
    const maximum = assertStable(held);
    assert(report.mediaRequests.some(request => request.case === caseName && request.camera === 'head' && request.heldWrites > 0), 'No head bytes were actually withheld.');
    return {frozenAtSeconds: held[0].time, maxPresentedGapDuringFreezeSeconds: maximum};
  };
  const saveEvents = async () => {report.events.push({case: caseName, events: await evaluate('syncEvents'), waits: await evaluate('syncWaitCalls')});};

  await call('Runtime.enable'); await call('Page.enable'); await call('Network.enable');
  await call('Emulation.setDeviceMetricsOverride', {width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false});

  if (process.env.F1_VIDEO_SYNC_CASE !== 'speed-only') {
  await open('head-outage-seek-resume');
  const first = await outage();
  await evaluate(`{const seek=document.querySelector('[data-media-seek]');seek.value='18';seek.dispatchEvent(new Event('input',{bubbles:true}));}`);
  await wait(800); const seeking = await sample();
  assert(Math.abs(seeking.time - 18) <= .05, 'The shared seek target was not retained during buffering.');
  assert(seeking.videos.every(video => video.paused), 'A side camera played while the head seek was blocked.');
  assert(seeking.videos.find(video => video.camera === 'head').frame.mediaTime < 17.8, 'The withheld seek unexpectedly had a decoded head frame; fixture no longer tests unbuffered seeking.');
  setHeadHeld(false);
  await until("Object.keys(syncFrames).length===3&&Object.values(syncFrames).every(f=>f.mediaTime>=18.4)&&[...document.querySelectorAll('[data-media-viewer] video')].every(v=>!v.paused)", 'group resumes after real seek frame availability');
  first.maxPresentedGapAfterResumeSeconds = assertStable(await collect(1200));
  first.bufferingSeekTargetSeconds = 18; first.autoResumed = true;
  report.checks.push(Object.assign({name: caseName}, first)); await click('[data-media-play]'); await saveEvents();

  await open('user-pause-during-outage');
  const second = await outage(); await click('[data-media-play]');
  const pausedAt = await sample();
  assert.strictEqual(pausedAt.button.trim(), '播放', 'Explicit user pause must clear playback intent.');
  setHeadHeld(false); await wait(1500);
  const paused = await collect(700);
  assert(paused.every(value => value.videos.every(video => video.paused)), 'Restored data restarted playback after the user paused.');
  assert(paused.every(value => Math.abs(value.time - pausedAt.time) <= .05), 'User-paused timeline moved after recovery.');
  second.userPausePreserved = true; second.maxPresentedGapAfterPausedRecoverySeconds = assertStable(paused);
  report.checks.push(Object.assign({name: caseName}, second)); await saveEvents();
  } else {await open('speed-only'); await click('[data-media-play]');}
  caseName = 'rapid-seek-speed-and-tail';
  await evaluate(`{const seek=document.querySelector('[data-media-seek]');seek.value='10';seek.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('[data-media-play]').click();}`);
  await until("Object.values(syncFrames).length===3&&Object.values(syncFrames).every(f=>f.mediaTime>=10.3&&f.mediaTime<12)", 'rapid pause seek play');
  const speedChecks = [];
  for (const rate of [.25, 2]) {
    await evaluate(`{const select=document.querySelector('[data-media-speed]');select.value='${rate}';select.dispatchEvent(new Event('change',{bubbles:true}));}`);
    const warmup = await collect(600);
    await until("[...document.querySelectorAll('[data-media-viewer] video')].every(v=>!v.paused&&v.readyState>=3&&Math.abs(v.currentTime-syncFrames[v.dataset.camera].mediaTime)<.1)", 'decoder settles at ' + rate + 'x');
    const samples = await collect(800), gap = assertStable(samples);
    const elapsed = (samples[samples.length - 1].browserAtMs - samples[0].browserAtMs) / 1000;
    const advanced = samples[samples.length - 1].videos[0].frame.mediaTime - samples[0].videos[0].frame.mediaTime;
    assert(Math.abs(advanced - elapsed * rate) < .15, 'Playback speed did not apply uniformly at ' + rate + 'x.');
    speedChecks.push({rate, warmupMaxPresentedGapSeconds: Math.max(...warmup.map(spread)),
      warmupPausedSamples: warmup.filter(value => value.videos.some(video => video.paused)).length,
      elapsedSeconds: elapsed, presentedAdvanceSeconds: advanced, maxPresentedGapSeconds: gap});
  }
  await click('[data-media-play]');
  await evaluate(`{const seek=document.querySelector('[data-media-seek]');seek.value='24';seek.dispatchEvent(new Event('input',{bubbles:true}));}`);
  await until("Object.values(syncFrames).length===3&&Object.values(syncFrames).every(f=>f.mediaTime>23.9)&&[...document.querySelectorAll('[data-media-viewer] video')].every(v=>v.paused)", 'paused seek displays final frames');
  report.checks.push({name: caseName, rapidPauseSeekPlay: true, speedChecks, finalPresentedGapSeconds: assertStable(await collect(300))});
  await saveEvents();

  assert.strictEqual(report.blockedWrites.length, 0, 'The viewer attempted a write API.');
  assert.strictEqual(report.exceptions.length, 0, 'Unexpected browser exception.');
  report.passed = true;
}

main().catch(async error => {
  report.passed = false; report.failure = error.stack || String(error);
  if (debugCall) {
    try {const diagnostic = await debugCall('Runtime.evaluate', {expression: '({events:window.syncEvents,waits:window.syncWaitCalls})', returnByValue: true}); report.failureMediaTrace = diagnostic.result.value;} catch (_) {}
    try {const shot = await debugCall('Page.captureScreenshot', {format: 'png'}); fs.writeFileSync(path.join(output, 'failure.png'), Buffer.from(shot.data, 'base64'));} catch (_) {}
  }
  process.exitCode = 1;
}).finally(async () => {
  fs.mkdirSync(output, {recursive: true}); fs.writeFileSync(path.join(output, 'summary.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({passed: report.passed, checks: report.checks, failure: report.failure, output}));
  if (ws) ws.close();
  if (chrome) {chrome.kill(); await Promise.race([new Promise(resolve => chrome.once('exit', resolve)), wait(1500)]);}
  if (server) server.close();
  for (const socket of sockets) socket.destroy();
  if (browserProfile) {try {
    if (fs.rmSync) fs.rmSync(browserProfile, {recursive: true, force: true});
    else fs.rmdirSync(browserProfile, {recursive: true});
  } catch (_) {}}
});

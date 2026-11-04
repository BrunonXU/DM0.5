/* Actual read-only reader browser acceptance. Uses a supplied WhyHow URL.
 * No frontend edits, robot connections, training or application write requests. */
const http = require('http'), fs = require('fs'), path = require('path');
const cp = require('child_process'), os = require('os'), assert = require('assert');
const WebSocket = require('ws');
const backend = process.env.WHYHOW_TEST_URL || 'http://127.0.0.1:18443';
let base = backend;
const root = path.resolve(__dirname, '..');
const output = process.env.WHYHOW_BROWSER_OUTPUT || path.join(root, 'artifacts/browser');
const bridge = process.env.WHYHOW_CDP_HTTP_BRIDGE === '1';
const routesOnly = process.env.WHYHOW_ROUTES_ONLY === '1';
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
function get(url) {
  return new Promise((resolve, reject) => {
    http.get(url, res => {
      let body = '';
      res.on('data', b => body += b);
      res.on('end', () => {
        if (res.statusCode !== 200) return reject(Error(url + ': HTTP ' + res.statusCode));
        try { resolve(JSON.parse(body)); } catch (e) { reject(e); }
      });
    }).on('error', reject);
  });
}
async function session(url, events) {
  const socket = new WebSocket(url);
  await new Promise((resolve, reject) => { socket.on('open', resolve); socket.on('error', reject); });
  let counter = 0;
  const pending = new Map();
  socket.on('message', raw => {
    const value = JSON.parse(raw);
    if (value.id && pending.has(value.id)) {
      const item = pending.get(value.id);
      clearTimeout(item.timeout); pending.delete(value.id);
      value.error ? item.reject(Error(JSON.stringify(value.error))) : item.resolve(value.result);
    } else if (value.method) events(value);
  });
  function call(method, params) {
    return new Promise((resolve, reject) => {
      const id = ++counter;
      const timeout = setTimeout(() => { pending.delete(id); reject(Error('CDP timeout: ' + method)); }, method === 'Page.navigate' ? 90000 : 25000);
      pending.set(id, {resolve, reject, timeout});
      socket.send(JSON.stringify({id, method, params: params || {}}));
    });
  }
  async function evaluate(expression, contextId) {
    const params = {expression, returnByValue: true, awaitPromise: true, userGesture: true};
    if (contextId !== undefined) params.contextId = contextId;
    const reply = await call('Runtime.evaluate', params);
    if (reply.exceptionDetails) throw Error(JSON.stringify(reply.exceptionDetails));
    return reply.result.value;
  }
  return {call, evaluate, close: () => socket.close()};
}
(async () => {
  fs.mkdirSync(output, {recursive: true});
  let proxy;
  if (process.env.WHYHOW_NODE_PROXY === '1') {
    proxy = http.createServer((request, response) => {
      if (request.method !== 'GET' && request.method !== 'HEAD') { response.statusCode = 405; response.end(); return; }
      const remote = http.get(backend + request.url, upstream => {
        response.writeHead(upstream.statusCode, upstream.headers); upstream.pipe(response);
      });
      remote.on('error', error => { response.statusCode = 502; response.end(String(error)); });
    });
    await new Promise(resolve => proxy.listen(0, '127.0.0.1', resolve));
    base = 'http://127.0.0.1:' + proxy.address().port;
  }
  const catalog = await get(base + '/api/catalog');
  assert.equal(catalog.items.length, 8, 'Expected the eight registered catalog entries');
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'whyhow-browser-'));
  const stderr = fs.openSync(path.join(output, 'chrome-stderr.log'), 'w');
  const chrome = cp.spawn(process.env.CHROME_BIN || '/usr/bin/google-chrome',
    ['--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--disable-popup-blocking',
      '--no-proxy-server', '--disable-background-networking', '--disable-component-update', '--disable-gpu',
      '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'], {stdio: ['ignore', 'ignore', stderr]});
  const report = {started_at: new Date().toISOString(), scope: routesOnly ? 'iframe and navigation regression' : 'full desktop reader acceptance', url: base, backend, proxy: Boolean(proxy), catalog: catalog.items.map(item => ({id: item.id, kind: item.kind})),
    checks: [], documents: [], screenshots: [], javascript_exceptions: [],
    failed_responses: [], writes: [], external_requests: [], production_ui_modified: false,
    transport: bridge ? 'CDP request bridge to the actual local HTTP backend; native Chrome HTTP transport not certified' : 'native Chrome HTTP'};
  let client, browser, iframeClient;
  function record(name, detail) { report.checks.push({name, detail}); console.log('PASS ' + name); }
  try {
    const active = path.join(profile, 'DevToolsActivePort');
    for (let i = 0; i < 100 && !fs.existsSync(active); i++) await wait(100);
    const debugPort = fs.readFileSync(active, 'utf8').split('\n')[0];
    const devtools = 'http://127.0.0.1:' + debugPort;
    const version = await get(devtools + '/json/version');
    const targets = await get(devtools + '/json/list');
    const target = targets.find(t => t.type === 'page');
    const contexts = new Map();
    client = await session(target.webSocketDebuggerUrl, event => {
      if (event.method === 'Fetch.requestPaused') {
        const paused = event.params, request = paused.request;
        if (!['GET', 'HEAD'].includes(request.method) || new URL(request.url).origin !== new URL(base).origin) {
          client.call('Fetch.failRequest', {requestId: paused.requestId, errorReason: 'BlockedByClient'}).catch(() => {});
          return;
        }
        http.get(request.url, response => {
          const chunks = [];
          response.on('data', chunk => chunks.push(chunk));
          response.on('end', () => {
            const headers = Object.entries(response.headers).filter(([name]) => !['connection', 'transfer-encoding'].includes(name))
              .map(([name, value]) => ({name, value: Array.isArray(value) ? value.join(', ') : String(value)}));
            client.call('Fetch.fulfillRequest', {requestId: paused.requestId, responseCode: response.statusCode,
              responseHeaders: headers, body: Buffer.concat(chunks).toString('base64')}).catch(() => {});
          });
        }).on('error', () => client.call('Fetch.failRequest', {requestId: paused.requestId, errorReason: 'ConnectionFailed'}).catch(() => {}));
      }
      if (event.method === 'Runtime.exceptionThrown') report.javascript_exceptions.push(event.params.exceptionDetails);
      if (event.method === 'Runtime.executionContextCreated') {
        const context = event.params.context; contexts.set(context.id, context);
      }
      if (event.method === 'Runtime.executionContextDestroyed') contexts.delete(event.params.executionContextId);
      if (event.method === 'Runtime.executionContextsCleared') contexts.clear();
      if (event.method === 'Network.requestWillBeSent') {
        const request = event.params.request;
        if (process.env.WHYHOW_DEBUG_REQUESTS === '1') console.log('HTTP ' + request.method + ' ' + request.url);
        if (!['GET', 'HEAD'].includes(request.method)) report.writes.push({url: request.url, method: request.method});
        if (request.url.startsWith('http') && new URL(request.url).origin !== new URL(base).origin)
          report.external_requests.push(request.url);
      }
      if (event.method === 'Network.responseReceived' && event.params.response.status >= 400) {
        const res = event.params.response;
        if (!res.url.endsWith('/favicon.ico')) report.failed_responses.push({url: res.url, status: res.status});
      }
    });
    const {call, evaluate} = client;
    async function until(expression, label, contextId) {
      for (let i = 0; i < 120; i++) {
        if (await evaluate(expression, contextId)) return;
        await wait(100);
      }
      throw Error('Timed out: ' + label);
    }
    async function screenshot(name) {
      const image = await call('Page.captureScreenshot', {format: 'png'});
      fs.writeFileSync(path.join(output, name + '.png'), Buffer.from(image.data, 'base64'));
      report.screenshots.push(name + '.png');
    }
    async function openDocument(id) {
      await call('Page.navigate', {url: base + '/?doc=' + encodeURIComponent(id)});
      await until("document.getElementById('document')?.hidden===false && document.querySelectorAll('#markdown-body h1').length>0 && document.querySelectorAll('#markdown-body pre > code.language-mermaid').length===0", 'document ' + id);
      assert(await evaluate("document.getElementById('error').hidden"), 'Visible document error');
    }
    async function checkReaderControls() {
    await evaluate("document.getElementById('open-library').click()");
    assert(await evaluate("document.getElementById('library-dialog').open && document.activeElement.id==='search'"));
    await screenshot('directory-1440x900');
    await call('Input.dispatchKeyEvent', {type:'keyDown', key:'Escape', code:'Escape', windowsVirtualKeyCode:27});
    await call('Input.dispatchKeyEvent', {type:'keyUp', key:'Escape', code:'Escape', windowsVirtualKeyCode:27});
    await until("!document.getElementById('library-dialog').open && document.activeElement.id==='open-library'", 'drawer Escape focus return');
    await evaluate("document.dispatchEvent(new KeyboardEvent('keydown',{key:'k',ctrlKey:true,bubbles:true}))");
    assert(await evaluate("document.getElementById('library-dialog').open"));
    await evaluate("document.querySelector('#catalog .catalog-item').click()");
    await until("!document.getElementById('library-dialog').open && location.search.includes('v1-fullsft') && !document.getElementById('document').hidden", 'drawer article navigation');
    await evaluate("document.querySelector('[data-font=large]').click()");
    assert.equal(await evaluate("getComputedStyle(document.getElementById('markdown-body')).fontSize"), '24px');
    await evaluate("document.querySelector('[data-font=standard]').click()");
    record('directory open, Escape/focus, shortcut, selection close and font controls', {});
    }
    await call('Page.enable'); await call('Runtime.enable'); await call('Network.enable');
    console.log('Chrome initial runtime: ' + await evaluate('location.href'));
    if (bridge) await call('Fetch.enable', {patterns: [{urlPattern: base + '/*', requestStage: 'Request'}]});
    await call('Page.navigate', {url: base + '/?html=action-windows'});
    await until("document.getElementById('html-count')?.textContent==='2' && document.getElementById('markdown-count')?.textContent==='6' && document.getElementById('interactive')?.hidden===false", 'catalog and HTML entry');
    await call('Emulation.setDeviceMetricsOverride', {width: 1440, height: 900, deviceScaleFactor: 1, mobile: false});
    record('eight catalog entries grouped by reading mode', {html: 2, markdown: 6});

    // The HTML is intentionally sandboxed without same-origin permission.
    await until("document.getElementById('iframe-status').hidden", 'interactive iframe loaded');
    const tree = await call('Page.getFrameTree');
    function findFrame(node) {
      if (node.frame.url.includes('/interactive/action-windows/')) return node.frame;
      for (const child of node.childFrames || []) { const result = findFrame(child); if (result) return result; }
    }
    const frame = findFrame(tree.frameTree);
    let evaluateFrame;
    if (frame) {
      const isolated = await call('Page.createIsolatedWorld', {frameId: frame.id, worldName: 'whyhow-test'});
      evaluateFrame = expression => evaluate(expression, isolated.executionContextId);
    } else {
      const targets = (await call('Target.getTargets')).targetInfos;
      const target = targets.find(t => t.type === 'iframe' && t.url.includes('/interactive/action-windows/'));
      assert(target, 'Sandboxed interactive iframe target not found');
      iframeClient = await session('ws://127.0.0.1:' + debugPort + '/devtools/page/' + target.targetId, event => {
        if (event.method === 'Runtime.exceptionThrown') report.javascript_exceptions.push(event.params.exceptionDetails);
      });
      await iframeClient.call('Runtime.enable');
      evaluateFrame = iframeClient.evaluate;
    }
    for (let i = 0; i < 100 && !await evaluateFrame("Boolean(document.querySelector('#start') && document.querySelector('#startLabel').textContent.length>0)"); i++) await wait(100);
    const before = await evaluateFrame("({value:document.querySelector('#start').value,label:document.querySelector('#startLabel').textContent,explanation:document.querySelector('#hypotheticalDetail').textContent})");
    await evaluateFrame("document.querySelector('#start').value='180';document.querySelector('#start').dispatchEvent(new Event('input',{bubbles:true}));");
    const after = await evaluateFrame("({value:document.querySelector('#start').value,label:document.querySelector('#startLabel').textContent,explanation:document.querySelector('#hypotheticalDetail').textContent,cells:document.querySelectorAll('#window>*').length})");
    assert.equal(after.value, '180'); assert.notEqual(before.label, after.label);
    assert.notEqual(before.explanation, after.explanation); assert.equal(after.cells, 50);
    assert.equal(await evaluate("document.getElementById('interactive-frame').getAttribute('sandbox')"), 'allow-scripts');
    record('sandboxed real HTML slider updates 50-step window and explanation', {before, after});
    assert.equal(await evaluateFrame("getComputedStyle(document.body).fontSize"), '18px');
    assert.equal(await evaluateFrame("getComputedStyle(document.body).backgroundColor"), 'rgb(253, 253, 252)');
    record('shared theme reaches sandboxed interactive lesson', {});
    const htmlLayout = await evaluate("({kind:document.body.dataset.kind,sidebar:getComputedStyle(document.querySelector('.reading-tools')).display,iframeWidth:document.getElementById('interactive-frame').clientWidth,overflow:document.documentElement.scrollWidth>innerWidth})");
    assert.equal(htmlLayout.kind, 'html'); assert.equal(htmlLayout.sidebar, 'none');
    assert(htmlLayout.iframeWidth > 1050); assert(!htmlLayout.overflow);
    record('HTML mode expands real iframe across desktop reading surface', htmlLayout);
    await screenshot('interactive-1440x900');
    for (const size of [[1920, 1080], [1280, 800]]) {
      await call('Emulation.setDeviceMetricsOverride', {width: size[0], height: size[1], deviceScaleFactor: 1, mobile: false});
      assert(await evaluate("document.documentElement.scrollWidth<=innerWidth"), 'Outer page horizontal overflow');
      await screenshot('interactive-' + size.join('x'));
    }
    await call('Emulation.setDeviceMetricsOverride', {width: 1440, height: 900, deviceScaleFactor: 1, mobile: false});
    await evaluate("document.getElementById('tab-markdown').click()");
    await until("document.querySelectorAll('#catalog .catalog-item').length===6 && !document.getElementById('document').hidden", 'Markdown catalog click');
    record('reading-mode switch opens Markdown and six catalog buttons', {});
    if (routesOnly) {
      await openDocument('window-walkthrough');
      const previous = await evaluate('location.href');
      const next = await evaluate("(()=>{const a=[...document.querySelectorAll('#markdown-body a[href]')].find(a=>a.getAttribute('href').startsWith('/?path='));if(!a)return null;const href=a.href;a.click();return href})()");
      assert(next, 'A real relative Markdown link is required');
      await until("location.href===" + JSON.stringify(next) + "&&document.getElementById('document')?.hidden===false", 'relative document link');
      await wait(300);
      report.history_source = {previous, next};
      report.history_before_back = await call('Page.getNavigationHistory');
      await evaluate('history.back()');
      await until("location.href===" + JSON.stringify(previous) + "&&document.getElementById('document')?.hidden===false", 'browser back to source Markdown');
      report.history_after_back = await call('Page.getNavigationHistory');
      await evaluate('history.forward()');
      await until("location.href===" + JSON.stringify(next) + "&&document.getElementById('document')?.hidden===false", 'browser forward to linked Markdown');
      record('one browser back and forward restores the two real Markdown documents', {previous, next});
      await evaluate("document.getElementById('tab-html').click()");
      await until("document.getElementById('interactive')?.hidden===false&&document.getElementById('iframe-status')?.hidden===true&&document.getElementById('interactive-frame')?.src.includes('/interactive/action-windows/')", 'HTML frame recreated from Markdown');
      const htmlUrl = await evaluate('location.href');
      await evaluate("document.getElementById('tab-markdown').click()");
      await until("document.getElementById('document')?.hidden===false&&!document.getElementById('interactive-frame')?.hasAttribute('src')", 'Markdown clears old frame without navigation');
      const markdownUrl = await evaluate('location.href');
      await evaluate('history.back()');
      await until("location.href===" + JSON.stringify(htmlUrl) + "&&document.getElementById('interactive')?.hidden===false&&document.getElementById('iframe-status')?.hidden===true", 'back from Markdown to loaded HTML');
      await evaluate('history.forward()');
      await until("location.href===" + JSON.stringify(markdownUrl) + "&&document.getElementById('document')?.hidden===false", 'forward from HTML to Markdown');
      record('HTML to Markdown and browser back/forward recreate the correct iframe and document', {htmlUrl, markdownUrl});
      await evaluate("document.getElementById('search').value='FullSFT';document.getElementById('search').dispatchEvent(new Event('input',{bubbles:true}))");
      await until("document.querySelectorAll('#catalog .catalog-item').length===1", 'catalog search');
      record('catalog search narrows to exact FullSFT article', {});
      await checkReaderControls();
      assert.equal(report.javascript_exceptions.length, 0, 'JavaScript errors');
      assert.equal(report.failed_responses.length, 0, 'Failed resource/API responses');
      assert.equal(report.writes.length, 0, 'Application write requests');
      assert.equal(report.external_requests.length, 0, 'Unexpected third-party requests');
      report.result = 'PASS';
      return;
    }
    for (const item of catalog.items.filter(it => it.kind === 'markdown')) {
      await openDocument(item.id);
      const details = await evaluate("({tables:document.querySelectorAll('#markdown-body table').length,codes:document.querySelectorAll('#markdown-body pre').length,diagrams:document.querySelectorAll('#markdown-body .mermaid-diagram svg').length,fallbacks:document.querySelectorAll('#markdown-body .diagram-fallback').length,toc:document.querySelectorAll('#toc a').length,images:[...document.querySelectorAll('#markdown-body img')].map(i=>({src:i.src,complete:i.complete,width:i.naturalWidth})),external:[...document.querySelectorAll('#markdown-body a[href^=\"http\"]')].filter(a=>new URL(a.href).origin!==location.origin).map(a=>({href:a.href,target:a.target,rel:a.rel}))})");
      await until("[...document.querySelectorAll('#markdown-body img')].every(i=>i.complete&&i.naturalWidth>0)", 'all document images ' + item.id);
      details.images = await evaluate("[...document.querySelectorAll('#markdown-body img')].map(i=>({src:i.src,width:i.naturalWidth,height:i.naturalHeight}))");
      assert(details.toc > 0); assert.equal(details.fallbacks, 0, 'Mermaid fallback in ' + item.id);
      for (const link of details.external) {
        assert.equal(link.target, '_blank'); assert(link.rel.includes('noopener') && link.rel.includes('noreferrer'));
      }
      report.documents.push({id: item.id, details});
    }
    assert(report.documents.some(d => d.details.tables > 0));
    assert(report.documents.some(d => d.details.codes > 0));
    assert(report.documents.some(d => d.details.diagrams > 0));
    assert(report.documents.some(d => d.details.images.length > 0));
    record('all six real documents, Markdown tables/code/Mermaid/images and external link safety attributes', {});

    await openDocument('v1-fullsft');
    for (const size of [[1440, 900], [1920, 1080], [1280, 800]]) {
      await call('Emulation.setDeviceMetricsOverride', {width: size[0], height: size[1], deviceScaleFactor: 1, mobile: false});
      const layout = await evaluate("({overflow:document.documentElement.scrollWidth>innerWidth,readingWidth:document.getElementById('reading-area').clientWidth,catalogVisible:document.getElementById('catalog').getBoundingClientRect().width>0,tocVisible:document.getElementById('toc').getBoundingClientRect().width>0})");
      assert(!layout.overflow); assert(layout.readingWidth > 500); assert(!layout.catalogVisible && layout.tocVisible);
      assert.equal(await evaluate("getComputedStyle(document.getElementById('markdown-body')).fontSize"), '22px');
      await screenshot('markdown-' + size.join('x'));
      record('desktop layout ' + size.join('x'), layout);
    }
    await call('Emulation.setDeviceMetricsOverride', {width: 1440, height: 900, deviceScaleFactor: 1, mobile: false});
    const anchor = await evaluate("(()=>{const a=document.querySelectorAll('#toc a')[4];a.click();return a.dataset.anchor})()");
    report.toc_click = await evaluate("({anchor:" + JSON.stringify(anchor) + ",hash:decodeURIComponent(location.hash.slice(1)),scroll:document.getElementById('reading-area').scrollTop,scrollHeight:document.getElementById('reading-area').scrollHeight,clientHeight:document.getElementById('reading-area').clientHeight})");
    await until("document.getElementById('reading-area').scrollTop>100 && decodeURIComponent(location.hash.slice(1))===" + JSON.stringify(anchor), 'TOC scroll and URL');
    record('chapter link updates scroll and shareable fragment', {anchor});
    browser = await session(version.webSocketDebuggerUrl, () => {});
    await browser.call('Browser.grantPermissions', {origin: base, permissions: ['clipboardReadWrite', 'clipboardSanitizedWrite']});
    await call('Page.bringToFront');
    await call('Emulation.setFocusEmulationEnabled', {enabled: true});
    report.clipboard_context = await evaluate("({focused:document.hasFocus(),secure:window.isSecureContext,disabled:document.getElementById('copy-link').disabled})");
    await evaluate("document.getElementById('copy-link').click()");
    await until("document.getElementById('toast').textContent==='当前阅读链接已复制'", 'copy-link success');
    const clipboard = await evaluate('navigator.clipboard.readText()');
    assert.equal(clipboard, await evaluate('location.href'));
    browser.close(); browser = null;
    record('copy link contains exact document URL and chapter fragment', {url: clipboard});

    await openDocument('training-50k');
    const imageSelector = '#markdown-body img';
    await until("document.querySelector(" + JSON.stringify(imageSelector) + ")?.naturalWidth>0", 'training curve image');
    await evaluate("document.querySelector('#markdown-body img').click()");
    await until("document.getElementById('image-dialog').open && document.getElementById('full-image').naturalWidth>0", 'image modal');
    await screenshot('image-detail-1440x900');
    await evaluate("document.getElementById('close-image').click()");
    assert(!await evaluate("document.getElementById('image-dialog').open"));
    record('real image opens and closes at full resolution', {});

    await openDocument('window-walkthrough');
    const previous = await evaluate('location.href');
    const localLink = await evaluate("(()=>{const a=[...document.querySelectorAll('#markdown-body a[href]')].find(a=>new URL(a.href).origin===location.origin&&a.getAttribute('href').startsWith('/?path='));if(!a)return null;const href=a.href;a.click();return href})()");
    assert(localLink, 'A real relative Markdown link is required');
    await until("location.href===" + JSON.stringify(localLink) + "&&document.getElementById('document')?.hidden===false", 'relative document link');
    const next = await evaluate('location.href');
    report.history_before_back = await call('Page.getNavigationHistory');
    report.history_source = {previous, next};
    await evaluate('history.back()');
    await until("location.href===" + JSON.stringify(previous) + "&&document.getElementById('document')?.hidden===false", 'browser back');
    await evaluate('history.forward()');
    await until("location.href===" + JSON.stringify(next) + "&&document.getElementById('document')?.hidden===false", 'browser forward');
    record('real relative Markdown link, browser back and forward', {previous, next});
    await evaluate("document.getElementById('search').value='FullSFT';document.getElementById('search').dispatchEvent(new Event('input',{bubbles:true}))");
    await until("document.querySelectorAll('#catalog .catalog-item').length===1", 'catalog search');
    record('catalog search narrows to exact FullSFT article', {});
    await checkReaderControls();
    assert.equal(report.javascript_exceptions.length, 0, 'JavaScript errors');
    assert.equal(report.failed_responses.length, 0, 'Failed resource/API responses');
    assert.equal(report.writes.length, 0, 'Application write requests');
    assert.equal(report.external_requests.length, 0, 'Unexpected third-party requests');
    report.result = 'PASS';
  } catch (error) {
    report.result = 'FAIL'; report.error = error.stack || String(error);
    if (client) {
      try {
        report.failure_state = await client.evaluate("(()=>{const a=document.getElementById('reading-area');return {href:location.href,kind:document.body.dataset.kind,historyLength:history.length,frameSrc:document.getElementById('interactive-frame')?.getAttribute('src'),documentHidden:document.getElementById('document')?.hidden,toast:document.getElementById('toast')?.textContent,focused:document.hasFocus(),reading:a&&{scroll:a.scrollTop,height:a.clientHeight,scrollHeight:a.scrollHeight},window:{width:innerWidth,height:innerHeight}}})()");
        report.failure_history = await client.call('Page.getNavigationHistory');
        const failed = await client.call('Page.captureScreenshot', {format: 'png'});
        fs.writeFileSync(path.join(output, 'failure.png'), Buffer.from(failed.data, 'base64'));
      } catch (captureError) { report.failure_capture_error = String(captureError); }
    }
    console.error(report.error); process.exitCode = 1;
  } finally {
    report.finished_at = new Date().toISOString();
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n');
    if (iframeClient) iframeClient.close();
    if (client) client.close(); if (browser) browser.close();
    chrome.kill('SIGTERM');
    if (proxy) proxy.close();
    fs.closeSync(stderr);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

/* Independent read-only Chrome transport probe. Owns and closes only its Chrome. */
const fs = require('fs'), path = require('path'), os = require('os');
const http = require('http'), cp = require('child_process'), WebSocket = require('ws');
const out = path.resolve(__dirname, '../artifacts/' + (process.env.WHYHOW_PROBE_NAME || 'navigation-probe'));
fs.mkdirSync(out, {recursive: true});
const sleep = ms => new Promise(r => setTimeout(r, ms));
const log = [];
function record(name, value) {
  const line = {at: new Date().toISOString(), name, value};
  log.push(line); console.log(JSON.stringify(line));
  fs.writeFileSync(path.join(out, 'report.json'), JSON.stringify(log, null, 2));
}
function get(url) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, res => {
      let body = ''; res.on('data', b => body += b);
      res.on('end', () => resolve({status: res.statusCode, body}));
    });
    req.on('error', reject); req.setTimeout(5000, () => req.destroy(Error('HTTP timeout')));
  });
}
async function connect(url) {
  const ws = new WebSocket(url), pending = new Map(); let next = 0;
  await new Promise((resolve,reject) => {ws.once('open',resolve);ws.once('error',reject);});
  ws.on('message', raw => {
    const m = JSON.parse(raw);
    if (pending.has(m.id)) {
      const p = pending.get(m.id); pending.delete(m.id); clearTimeout(p.timer);
      m.error ? p.reject(Error(JSON.stringify(m.error))) : p.resolve(m.result);
    } else if (m.method && /requestWillBeSent|loadingFailed|responseReceived|frameNavigated|frameStartedLoading|frameStoppedLoading|exceptionThrown/.test(m.method)) {
      record(m.method, m.params);
    }
  });
  return {
    ws,
    call(method, params) {
      return new Promise((resolve, reject) => {
        const id = ++next;
        const timer = setTimeout(() => {pending.delete(id); reject(Error('CDP timeout '+method));}, Number(process.env.WHYHOW_PROBE_TIMEOUT || 7000));
        pending.set(id,{resolve,reject,timer}); ws.send(JSON.stringify({id,method,params:params||{}}));
      });
    }
  };
}
(async () => {
  const tiny = http.createServer((req,res) => {
    record('tiny-request', req.url); res.writeHead(200,{'Content-Type':'text/html'});
    res.end('<!doctype html><title>Probe</title><p id="ready">ready</p>');
  });
  await new Promise(r => tiny.listen(0,'127.0.0.1',r));
  const tinyUrl = 'http://127.0.0.1:' + tiny.address().port + '/';
  const profile = fs.mkdtempSync(path.join(os.tmpdir(),'whyhow-probe-'));
  const args = ['--headless=new','--no-sandbox','--disable-dev-shm-usage','--no-proxy-server',
    '--disable-background-networking','--disable-component-update','--disable-gpu',
    '--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'];
  const err = fs.openSync(path.join(out,'chrome-stderr.log'),'w');
  const chrome = cp.spawn('/usr/bin/google-chrome',args,{stdio:['ignore','ignore',err]});
  record('chrome',{pid:chrome.pid,args});
  let client, browser;
  try {
    const active = path.join(profile,'DevToolsActivePort');
    for (let i=0;i<100&&!fs.existsSync(active);i++) await sleep(100);
    const port = fs.readFileSync(active,'utf8').split('\n')[0];
    if (process.env.WHYHOW_PROBE_BROWSER) {
      const version = JSON.parse((await get('http://127.0.0.1:'+port+'/json/version')).body);
      browser = await connect(version.webSocketDebuggerUrl);
    }
    const target = JSON.parse((await get('http://127.0.0.1:'+port+'/json/list')).body).find(t=>t.type==='page');
    client = await connect(target.webSocketDebuggerUrl);
    await client.call('Page.enable'); await client.call('Runtime.enable'); await client.call('Network.enable');
    record('about-blank-runtime',await client.call('Runtime.evaluate',{expression:'({href:location.href,ua:navigator.userAgent})',returnByValue:true}));
    if (process.env.WHYHOW_PROBE_EMULATION) {
      record('emulation', await client.call('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false}));
    }
    const urls = process.env.WHYHOW_PROBE_DIRECT ? ['http://127.0.0.1:4243/'] : ['data:text/html,<title>data-ready</title><p>data</p>',tinyUrl,'http://127.0.0.1:4243/api/catalog','http://127.0.0.1:4243/'];
    if (process.env.WHYHOW_PROBE_DATA_FIRST) urls.unshift('data:text/html,<title>warm-up</title><p>probe</p>');
    for (const url of urls) {
      record('navigate-start',url);
      if (url.startsWith('http')) {
        try { const r = await get(url); record('node-get',{url,status:r.status,bytes:r.body.length});}
        catch(e) {record('node-get-error',String(e));}
      }
      try {record('navigate-result',await client.call('Page.navigate',{url}));}
      catch(e) {record('navigate-error',String(e));}
      try {record('post-navigation-runtime',await client.call('Runtime.evaluate',{expression:'({href:location.href,title:document.title,state:document.readyState})',returnByValue:true}));}
      catch(e) {record('post-navigation-error',String(e));}
      await sleep(500);
    }
    if (process.env.WHYHOW_PROBE_LATE) {
      record('late-emulation',await client.call('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false}));
      const version = JSON.parse((await get('http://127.0.0.1:'+port+'/json/version')).body);
      browser = await connect(version.webSocketDebuggerUrl);
      record('late-runtime', await client.call('Runtime.evaluate',{expression:'({href:location.href,width:innerWidth,height:innerHeight})',returnByValue:true}));
      const screenshot = await client.call('Page.captureScreenshot',{format:'png'});
      fs.writeFileSync(path.join(out,'native-1440.png'),Buffer.from(screenshot.data,'base64'));
      record('late-screenshot','native-1440.png');
    }
  } catch(e) {record('fatal',String(e)); process.exitCode=1;}
  finally {
    if(client) client.ws.close();
    if(browser) browser.ws.close();
    chrome.kill('SIGTERM'); await sleep(500);
    if(chrome.exitCode===null) chrome.kill('SIGKILL');
    fs.closeSync(err); tiny.close(); record('finished',{chromeExit:chrome.exitCode});
  }
})().catch(e=>{record('outer-error',String(e));process.exitCode=1;});

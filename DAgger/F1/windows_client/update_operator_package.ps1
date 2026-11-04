param(
 [string]$RobotHost='user@192.0.2.7',
 [string]$ClientRoot=(Join-Path $HOME 'DAgger-F1'),
 [string]$PythonExe=$env:F1_SENDER_PYTHON,
 [switch]$SetupKey
)
$ErrorActionPreference='Stop'
function Assert-Idle {
 $running=Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match '(03_windows_(f1|shadow)_sender|windows_f1_(session|bootstrap))\.py' }
 if ($running) { throw '请先保存当前条并 Ctrl+C 退出操控窗口。' }
}
Assert-Idle
if (!$PythonExe) { $PythonExe=Join-Path $HOME '.venv/Scripts/python.exe' }
if (!(Test-Path -LiteralPath $PythonExe)) { throw '未找到原有Python，请指定-PythonExe；不改Python环境。' }
$stage=Join-Path $env:TEMP ('f1-operator-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage | Out-Null
$zip=Join-Path $stage 'update.zip'
$scpOptions=@('-F','NUL')
$operatorKey=Join-Path $HOME '.ssh/f1_operator_ed25519'
if (Test-Path -LiteralPath $operatorKey) { $scpOptions+=@('-i',$operatorKey,'-o','IdentitiesOnly=yes') }
& scp @scpOptions "${RobotHost}:/home/user/Bruno/DAgger/updates/__REMOTE__/windows-single-window-__VERSION__.zip" $zip
if ($LASTEXITCODE -ne 0) { throw '下载失败，原客户端未修改。' }
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant() -ne '__ZIP_SHA256__') { throw '下载包校验失败。' }
$source=Join-Path $stage 'files'
Expand-Archive -LiteralPath $zip -DestinationPath $source
# No sockets, controllers, threads or robot output in this probe.
$probe=@'
import hashlib, json, pathlib, runpy, sys
root = pathlib.Path(sys.argv[1]).resolve()
manifest = json.loads((root/'manifest.json').read_text())
assert len(manifest) == 14
for name, digest in manifest.items():
    path = (root/name).resolve()
    assert path.is_relative_to(root) and hashlib.sha256(path.read_bytes()).hexdigest() == digest, name
entry = runpy.run_path(str(root/'scripts/03_windows_f1_sender.py'))
entry['load_sender']()
for name, module in list(sys.modules.items()):
    if name == 'dagger_core' or name.startswith('dagger_core.'):
        assert pathlib.Path(module.__file__).resolve().is_relative_to(root/'src'), name
module = runpy.run_path(str(root/'scripts/windows_f1_session.py'))
identity = json.loads((root/'operator-package.json').read_text())
assert module['LAUNCHER_VERSION'] == identity['version'] == '__VERSION__'
assert identity['operator_protocol'] == 'f1_rlt_local_v3'
print('complete operator package verified; no processes started')
'@
& $PythonExe -I -B -c $probe $source
if ($LASTEXITCODE -ne 0) { throw '整包加载检查失败，原客户端未修改。' }
Assert-Idle
$destination=Join-Path $ClientRoot 'packages/f1-operator-__VERSION__'
if (Test-Path -LiteralPath $destination) { throw '版本目录已存在；请核对已安装版本，不覆盖冻结包。' }
New-Item -ItemType Directory -Force -Path (Split-Path $destination) | Out-Null
Copy-Item -LiteralPath $source -Destination $destination -Recurse
& $PythonExe -I -B -c $probe $destination
if ($LASTEXITCODE -ne 0) { throw '安装位置校验失败，未切换版本。' }
# One atomic pointer commits the complete dependency closure. The two stable
# bootstrap files are backed up and restored if installation fails.
$activate=@'
import hashlib, json, os, pathlib, shutil, sys, time
root, package = map(pathlib.Path, sys.argv[1:])
backup = root/'backups'/('operator-'+str(time.time_ns()))
backup.mkdir(parents=True)
names = ['scripts/windows_f1_bootstrap.py', 'start_f1_rlt.cmd', 'current-package.json']
existed = {n:(root/n).exists() for n in names}
for n in names:
    if existed[n]:
        (backup/n).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root/n, backup/n)
try:
    for n in names[:2]:
        p=root/n; p.parent.mkdir(parents=True, exist_ok=True)
        temporary=p.with_suffix(p.suffix+'.new'); shutil.copy2(package/n,temporary); os.replace(temporary,p)
    pointer={'directory':str(package.relative_to(root)), 'manifest_sha256':hashlib.sha256((package/'manifest.json').read_bytes()).hexdigest()}
    temp=root/'current-package.json.new'; temp.write_text(json.dumps(pointer),encoding='utf-8'); os.replace(temp,root/'current-package.json')
    (backup/'installed.json').write_text(json.dumps(pointer),encoding='utf-8')
except BaseException:
    for n in reversed(names):
        if existed[n]: shutil.copy2(backup/n,root/n)
        elif (root/n).exists(): (root/n).unlink()
    raise
print('installed __VERSION__; backup='+str(backup))
'@
Assert-Idle
& $PythonExe -I -B -c $activate $ClientRoot $destination
if ($LASTEXITCODE -ne 0) { throw '切换失败；检查备份和错误，勿启动混合版本。' }
Write-Host '操控包与sender __VERSION__已安装；无需Enter，真实START开条。本安装不启动动作。'
if ($SetupKey) {
 & $PythonExe (Join-Path $destination 'scripts/windows_f1_session.py') --setup-key --robot-host $RobotHost
 if ($LASTEXITCODE -ne 0) { throw '操控包已安装，但免密码设置尚未成功。' }
}

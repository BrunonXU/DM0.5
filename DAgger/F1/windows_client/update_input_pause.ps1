param(
 [string]$RobotHost='user@192.0.2.7',
 [string]$ClientRoot=(Join-Path $HOME 'DAgger-F1'),
 [string]$PythonExe=$env:F1_SENDER_PYTHON
)
$ErrorActionPreference='Stop'
function Assert-SenderStopped {
 $running=Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match '03_windows_(f1|shadow)_sender\.py' }
 if ($running) { throw '请先保存当前条并 Ctrl+C 停止 Windows sender；隧道可以保留。' }
}
Assert-SenderStopped
if (!(Test-Path -LiteralPath (Join-Path $ClientRoot 'scripts/windows_f1_sender.cmd'))) { throw "客户端路径不匹配: $ClientRoot" }
if (!$PythonExe) { $PythonExe=Join-Path $HOME '.venv/Scripts/python.exe' }
if (!(Test-Path -LiteralPath $PythonExe -PathType Leaf)) { throw "未找到 sender 的 Python: $PythonExe。请用 -PythonExe 指定现有解释器，客户端未修改。" }
$stage=Join-Path $env:TEMP ('f1-input-pause-r2-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage | Out-Null
$zip=Join-Path $stage 'update.zip'
& scp -F NUL "${RobotHost}:/home/user/Bruno/DAgger/updates/20261015-input-pause-v31-r2/windows-input-pause-v31-r2.zip" $zip
if ($LASTEXITCODE -ne 0) { throw '下载失败，客户端未修改。' }
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant() -ne '__ZIP_SHA256__') { throw '更新包哈希不匹配，客户端未修改。' }
$source=Join-Path $stage 'files'
Expand-Archive -LiteralPath $zip -DestinationPath $source
$manifest=Get-Content -Raw -LiteralPath (Join-Path $source 'manifest.json') | ConvertFrom-Json
$entries=@($manifest.PSObject.Properties)
if ($entries.Count -ne 9) { throw '更新清单不完整' }
foreach ($entry in $entries) {
 if ($entry.Name -notmatch '^(scripts/03_windows_(f1|shadow)_sender\.py|src/dagger_core/(__init__|protocol|async_transport|input_recovery|f710_mapping|gamepad_windows|sender_guidance)\.py)$') { throw '更新清单越界' }
 if ((Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $source $entry.Name)).Hash.ToLowerInvariant() -ne $entry.Value) { throw '源文件校验失败，客户端未修改。' }
}
# Imports only: no controller initialization, socket, thread, or robot output.
$probe=@'
import pathlib, runpy, sys
root = pathlib.Path(sys.argv[1]).resolve()
entry = runpy.run_path(str(root / 'scripts/03_windows_f1_sender.py'))
entry['load_sender']()
for name, module in list(sys.modules.items()):
    if name == 'dagger_core' or name.startswith('dagger_core.'):
        assert pathlib.Path(module.__file__).resolve().is_relative_to(root / 'src'), name
print('sender import check OK')
'@
& $PythonExe -I -B -c $probe $source
if ($LASTEXITCODE -ne 0) { throw '更新包加载检查失败，客户端未修改。' }
Assert-SenderStopped
$backup=Join-Path $ClientRoot ('backups/input-pause-r2-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $backup | Out-Null
$original=@{}
foreach ($entry in $entries) {
 $dst=Join-Path $ClientRoot $entry.Name
 $original[$entry.Name]=Test-Path -LiteralPath $dst
 if ($original[$entry.Name]) {
  $saved=Join-Path $backup $entry.Name
  New-Item -ItemType Directory -Force -Path (Split-Path $saved) | Out-Null
  Copy-Item -LiteralPath $dst -Destination $saved
 }
}
$original | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $backup 'original-files.json')
Copy-Item -LiteralPath (Join-Path $source 'manifest.json') -Destination (Join-Path $backup 'installed-manifest.json')
$touched=New-Object 'System.Collections.Generic.List[string]'
try {
 foreach ($entry in $entries) {
  $dst=Join-Path $ClientRoot $entry.Name
  New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
  $touched.Add($entry.Name)
  Copy-Item -LiteralPath (Join-Path $source $entry.Name) -Destination $dst -Force
  if ((Get-FileHash -Algorithm SHA256 -LiteralPath $dst).Hash.ToLowerInvariant() -ne $entry.Value) { throw '写入后校验失败' }
 }
 & $PythonExe -I -B -c $probe $ClientRoot
 if ($LASTEXITCODE -ne 0) { throw '安装后的 sender 加载检查失败' }
} catch {
 $failure=$_
 $rollbackErrors=@()
 foreach ($name in $touched) {
  try {
   $dst=Join-Path $ClientRoot $name
   if ($original[$name]) { Copy-Item -LiteralPath (Join-Path $backup $name) -Destination $dst -Force }
   elseif (Test-Path -LiteralPath $dst) { Remove-Item -LiteralPath $dst -Force }
  } catch { $rollbackErrors += $_.Exception.Message }
 }
 if ($rollbackErrors.Count) { throw "安装失败且回退未完成，勿启动 sender。备份: $backup；$failure；$rollbackErrors" }
 throw "安装失败，已恢复本次覆盖的文件：$failure；备份: $backup"
}
Write-Host "更新成功：Windows sender v31-r2；9 个文件校验及加载检查通过。备份: $backup"
Write-Host '沿用 scripts/windows_f1_sender.cmd。输入短暂停顿：松 RB、摇杆回中，等提示后再按 RB；START 结束本条。'

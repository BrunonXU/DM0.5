param(
    [string]$G2Host = "10.130.86.13",
    [string]$G2User = "agi",
    [int]$Port = 7904,
    [string]$IdentityFile = ""
)

$ErrorActionPreference = "Stop"
$sshArgs = @(
    "-N", "-T",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=2",
    "-o", "ServerAliveCountMax=2",
    "-L", "127.0.0.1:${Port}:127.0.0.1:${Port}"
)
if ($IdentityFile -ne "") {
    $sshArgs += @("-i", $IdentityFile)
}
$sshArgs += "${G2User}@${G2Host}"

Write-Host "Direct tunnel: Windows 127.0.0.1:${Port} -> G2 127.0.0.1:${Port}"
Write-Host "This server is not part of the runtime path. Ctrl-C closes the tunnel."
& ssh.exe @sshArgs
exit $LASTEXITCODE

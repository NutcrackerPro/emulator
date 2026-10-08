# Run inside your Windows guest, after reviewing this script.
# Downloads only Valve's official installer and opens its normal setup wizard.
# It does not accept Steam terms, collect credentials, or activate Windows.
[CmdletBinding()]
param([switch]$DiagnosticsOnly)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($env:OS -ne 'Windows_NT') {
    throw 'Run this script inside the Windows virtual machine.'
}

$runtimeDir = Join-Path $PSScriptRoot '.runtime'
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$graphics = @(Get-CimInstance Win32_VideoController | ForEach-Object {
    [ordered]@{ name = $_.Name; driver = $_.DriverVersion }
})
$steamPaths = @(
    (Join-Path ${env:ProgramFiles(x86)} 'Steam\steam.exe'),
    (Join-Path $env:ProgramFiles 'Steam\steam.exe')
)
$steamPath = $steamPaths | Where-Object { Test-Path $_ } | Select-Object -First 1
$report = [ordered]@{
    windows = $os.Caption
    architecture = $os.OSArchitecture
    processor = $cpu.Name
    graphics = $graphics
    steam_installed = [bool]$steamPath
    game_tested = $false
    note = 'Steam login and Blue Archive gameplay require a manual test. Windows ARM anticheat support is unverified.'
}
$reportPath = Join-Path $runtimeDir 'diagnostics.json'
$report | ConvertTo-Json -Depth 4 | Set-Content -Path $reportPath -Encoding UTF8
Write-Host "Local diagnostics saved to $reportPath"
Write-Host 'Diagnostics contain no Steam account, passwords, or Windows product key.'

if ($DiagnosticsOnly) { exit 0 }
if ($steamPath) {
    Write-Host 'Steam is installed. Opening it; sign in yourself in the Steam application.'
    Start-Process -FilePath $steamPath
    exit 0
}

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$installer = Join-Path $runtimeDir 'SteamSetup.exe'
Invoke-WebRequest -Uri 'https://cdn.akamai.steamstatic.com/client/installer/SteamSetup.exe' -OutFile $installer -UseBasicParsing
$signature = Get-AuthenticodeSignature -FilePath $installer
if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate -or $signature.SignerCertificate.Subject -notmatch 'Valve') {
    throw 'Steam installer signature was not a valid Valve signature. The installer has not been run.'
}
Write-Host 'Opening the official Steam setup wizard. Review its terms and complete installation yourself.'
Start-Process -FilePath $installer -Wait
$steamPath = $steamPaths | Where-Object { Test-Path $_ } | Select-Object -First 1
$report.steam_installed = [bool]$steamPath
$report | ConvertTo-Json -Depth 4 | Set-Content -Path $reportPath -Encoding UTF8
if ($steamPath) {
    Write-Host 'Steam installation detected. Sign in inside Steam and test a game to confirm it runs.'
} else {
    Write-Host 'Steam was not detected in the default folders. Setup may have been cancelled or a custom folder selected.'
}

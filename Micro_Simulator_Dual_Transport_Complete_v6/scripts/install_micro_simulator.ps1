<#
.SYNOPSIS
Installs the Micro dual-transport firmware and Windows relay tools into an
existing Onomondo SoftSIM west workspace.

.DESCRIPTION
The script is user-independent. By default, it uses:
  $env:USERPROFILE\onomondo-softsim-test

It replaces only the simulator application and relay-tool folders and clears
the short simulator build directory. It does not remove .west, Nordic SDK
modules, the Onomondo SoftSIM module, or C:\ss.
#>

param(
    [string]$Workspace = (Join-Path $env:USERPROFILE "onomondo-softsim-test"),
    [string]$BuildDir = "C:\b\micro_sim_relay"
)

$ErrorActionPreference = "Stop"

$PackageRoot = Split-Path -Parent $PSScriptRoot
$SourceApp = Join-Path $PackageRoot "applications\micro_simulator"
$SourceRelay = Join-Path $PackageRoot "tools\micro_relay"
$DestinationApp = Join-Path $Workspace "applications\micro_simulator"
$DestinationRelay = Join-Path $Workspace "tools\micro_relay"
$SoftSimModule = Join-Path $Workspace "modules\lib\onomondo-softsim"

Write-Host "Micro simulator dual-transport installer"
Write-Host "Package root: $PackageRoot"
Write-Host "Workspace:    $Workspace"
Write-Host "Build folder: $BuildDir"
Write-Host ""

if (-not (Test-Path $Workspace)) {
    throw "Workspace not found: $Workspace. Complete Document 1 first."
}
if (-not (Test-Path (Join-Path $Workspace ".west"))) {
    throw "The folder is not a west workspace: $Workspace"
}
if (-not (Test-Path $SoftSimModule)) {
    throw "Onomondo SoftSIM module not found: $SoftSimModule"
}
if (-not (Test-Path $SourceApp)) {
    throw "Package application folder not found: $SourceApp"
}
if (-not (Test-Path $SourceRelay)) {
    throw "Package relay folder not found: $SourceRelay"
}

Remove-Item Env:ZEPHYR_BASE -ErrorAction SilentlyContinue

Write-Host "Removing the previous simulator application..."
Remove-Item -Recurse -Force $DestinationApp -ErrorAction SilentlyContinue

Write-Host "Removing the previous simulator build..."
Remove-Item -Recurse -Force $BuildDir -ErrorAction SilentlyContinue

Write-Host "Installing firmware application..."
New-Item -ItemType Directory -Force (Split-Path -Parent $DestinationApp) | Out-Null
Copy-Item -Recurse -Force $SourceApp $DestinationApp

Write-Host "Installing Windows relay tools..."
Remove-Item -Recurse -Force $DestinationRelay -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force (Split-Path -Parent $DestinationRelay) | Out-Null
Copy-Item -Recurse -Force $SourceRelay $DestinationRelay

Write-Host ""
Write-Host "Installation complete."
Write-Host ""
Write-Host "Next, open the nRF Connect SDK v3.4 terminal in VS Code and run:"
Write-Host "  `$ws = Join-Path `$env:USERPROFILE 'onomondo-softsim-test'"
Write-Host "  Set-Location `$ws"
Write-Host "  Remove-Item Env:ZEPHYR_BASE -ErrorAction SilentlyContinue"
Write-Host "  west build --sysbuild -b nrf9151dk/nrf9151/ns -s applications\micro_simulator -d $BuildDir --pristine=always"
Write-Host "  west flash -d $BuildDir"
Write-Host ""
Write-Host "For Wi-Fi relay mode, open a normal Windows PowerShell window and run:"
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$DestinationRelay\setup_windows_relay.ps1`""
Write-Host ""
Write-Host "Then start the relay with:"
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$DestinationRelay\run_windows_relay.ps1`" -Port COM7"

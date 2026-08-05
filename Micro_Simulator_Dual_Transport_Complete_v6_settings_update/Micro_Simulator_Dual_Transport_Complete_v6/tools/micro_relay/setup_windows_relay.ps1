<#
Creates an isolated Python virtual environment for the Windows serial-to-TCP
relay. Run this from a normal Windows PowerShell window, not from the Nordic SDK
terminal.
#>

param(
    [switch]$Recreate
)

$ErrorActionPreference = "Stop"
$RelayDir = $PSScriptRoot
$VenvDir = Join-Path $RelayDir ".venv"
$Requirements = Join-Path $RelayDir "requirements.txt"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

$PyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
if (-not $PyLauncher) {
    throw "Windows Python launcher (py.exe) was not found. Install standard Python 3, enable the Python launcher, then rerun this script."
}

if ($Recreate -and (Test-Path $VenvDir)) {
    Remove-Item -Recurse -Force $VenvDir
}

if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating isolated Python environment at $VenvDir"
    & $PyLauncher.Source -3 -I -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        throw "Python virtual-environment creation failed."
    }
}

Write-Host "Installing relay dependencies..."
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }

& $VenvPython -m pip install -r $Requirements
if ($LASTEXITCODE -ne 0) { throw "Relay dependency installation failed." }

& $VenvPython -c "import serial; print('PySerial ready:', serial.__version__)"
if ($LASTEXITCODE -ne 0) { throw "PySerial verification failed." }

Write-Host ""
Write-Host "Relay environment is ready."
Write-Host "Run:"
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$RelayDir\run_windows_relay.ps1`" -Port COM7"

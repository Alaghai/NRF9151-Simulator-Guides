<#
Starts the Micro Windows serial-to-TCP relay using its isolated environment.
The relay window becomes the board's serial terminal, so close nRF Terminal
before running it.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$Port,
    [string]$ServerHost = "137.184.163.176",
    [int]$TcpPort = 5000,
    [int]$Baud = 115200
)

$ErrorActionPreference = "Stop"
$RelayDir = $PSScriptRoot
$Python = Join-Path $RelayDir ".venv\Scripts\python.exe"
$RelayScript = Join-Path $RelayDir "micro_serial_wifi_relay.py"

if (-not (Test-Path $Python)) {
    throw "Relay environment is not installed. Run setup_windows_relay.ps1 first."
}
if (-not (Test-Path $RelayScript)) {
    throw "Relay Python file not found: $RelayScript"
}

Write-Host "Close VS Code nRF Terminal before continuing."
Write-Host "Serial port: $Port"
Write-Host "TCP server:  $ServerHost`:$TcpPort"
Write-Host ""

& $Python $RelayScript --port $Port --baud $Baud --host $ServerHost --tcp-port $TcpPort
exit $LASTEXITCODE

$ErrorActionPreference = "Stop"
$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Relay environment is not installed. Run setup_windows_relay.ps1 first."
}
& $Python -m serial.tools.list_ports -v

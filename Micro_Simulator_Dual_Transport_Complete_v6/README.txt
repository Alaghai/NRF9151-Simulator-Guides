MICRO SIMULATOR DUAL-TRANSPORT PACKAGE - VERSION 6

nRF9151 DK + Onomondo SoftSIM + direct LTE or Windows Wi-Fi relay
Version 7 packet envelope + trusted-device heartbeat extension + settings updates

REPOSITORY USE
--------------
The simulator is intended to be stored as a normal folder in the GitHub
repository. Users do not need to download or extract a separate simulator ZIP.
From a cloned repository, run the installer from this folder's scripts
directory.

INSTALLED PATHS
---------------
Firmware application:
  $env:USERPROFILE\onomondo-softsim-test\applications\micro_simulator

Windows relay:
  $env:USERPROFILE\onomondo-softsim-test\tools\micro_relay

Short build directory:
  C:\b\micro_sim_relay

INSTALL
-------
Open standard Windows PowerShell from this repository folder:

  Set-Location .\scripts
  powershell -ExecutionPolicy Bypass -File .\install_micro_simulator.ps1

BUILD AND FLASH
---------------
Open the nRF Connect SDK v3.4 terminal:

  $ws = Join-Path $env:USERPROFILE "onomondo-softsim-test"
  Set-Location $ws
  Remove-Item Env:ZEPHYR_BASE -ErrorAction SilentlyContinue
  west build --sysbuild -b nrf9151dk/nrf9151/ns -s applications\micro_simulator -d C:\b\micro_sim_relay --pristine=always
  west flash -d C:\b\micro_sim_relay

MAIN CHANGES
------------
- Every dynamic heartbeat now appends the configured trusted-device registry:
  count + four fixed six-byte slots, with unused slots zero-filled.
- Original Version 7 heartbeat vectors remain available for regression tests.
- SETTINGS_UPDATE uses command 0x20 and a typed TLV payload.
- Persistent settings are stored as one validated Zephyr settings/NVS record.
- Serial commands: config show/generate/apply/set/reset/last.
- Direct LTE and relay response paths share fragmented OK/ERROR/SUP/FWUP parsing.
- SUP is followed by one binary settings packet.
- Automatic heartbeat scheduling uses heartbeat_interval_seconds.
- TCP server AUTO mode associates pending updates with an IMEI.
- Local packet, connection, and relay logs are expanded.
- Firmware image transfer and installation are not implemented.

PROTOCOL TESTS
--------------
From tools\protocol_test:

  python3 -m unittest -v

On Linux/macOS with a C compiler:

  ./run_c_settings_tests.sh

SERVER UPDATE TOOL
------------------
Example:

  python3 micro_update_tool.py queue --imei 861352064050787 --set heartbeat_interval_seconds=60

SECURITY
--------
Do not commit SoftSIM profiles, API keys, private keys, server passwords, IRKs,
or other credentials. The trusted-device heartbeat extension carries only the
configured six-byte non-secret identity/address representation used by the
simulator. It does not carry IRKs or bonding keys.

KNOWN COMPATIBILITY LIMITATION
------------------------------
The provided under-development firmware settings function parses a positional
payload. This package follows the attached implementation specification and
uses command 0x20 with TLV settings. Firmware must implement the same command
and TLV schema before the two implementations are wire-compatible.

FINAL TEST STATUS
-----------------
- 58 Python tests passed.
- Host C settings tests passed.
- config reset confirm generates a complete defaults update packet and applies
  it through the same validator/persistence path as serial and TCP updates.
- The Windows relay buffers fragmented SUP transactions until the complete
  Version 7 settings packet has arrived.

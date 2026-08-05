MICRO SIMULATOR PROJECT README - VERSION 6

nRF9151 DK + Onomondo SoftSIM + Direct LTE or Windows Wi-Fi Relay
Version 7 packet envelope with trusted-device heartbeat and settings-update extensions

DOCUMENT SERIES
---------------
1. 1_SoftSIM_and_Simulator_Setup_Guide_v6.docx
   Start-to-finish Windows setup, SDK/toolchain 3.4, Onomondo source
   workspace, GitHub-folder installation, optional SoftSIM provisioning,
   direct LTE, and Windows relay setup.

2. 2_Simulator_Protocol_Decisions_and_Usage_v7_SETTINGS_EXTENSION.docx
   Existing Version 7 envelope, heartbeat and location packets, appended
   trusted-device registry, command 0x20 settings updates, serial config
   commands, persistence, decoder behavior, and packet tests.

3. 3_Simulator_TCP_Server_and_Command_Guide_v7_SETTINGS_EXTENSION.docx
   TCP server deployment, binary/ASCII-HEX stream framing, AUTO mode,
   pending updates by IMEI, OK/ERROR/SUP/FWUP responses, local logs,
   update-generation commands, tests, and rollback.

GITHUB FOLDER INSTALLATION
--------------------------
The simulator is stored as a normal folder inside the GitHub repository. It
is no longer distributed to repository users as a separate simulator ZIP.

New repository copy:
  $Repo = Join-Path $env:USERPROFILE "NRF9151-Simulator-Guides"
  git clone https://github.com/Alaghai/NRF9151-Simulator-Guides.git $Repo

Update an existing repository copy:
  $Repo = Join-Path $env:USERPROFILE "NRF9151-Simulator-Guides"
  git -C $Repo pull

Run the installer from:
  $Repo\Micro_Simulator_Dual_Transport_Complete_v6\scripts

The ZIP supplied through ChatGPT is only a delivery container. After placing
its Micro_Simulator_Dual_Transport_Complete_v6 folder in GitHub, developers
use the normal repository folder and do not extract a separate package.

KEY SETUP PATHS
---------------
Workspace:
  $Workspace = Join-Path $env:USERPROFILE "onomondo-softsim-test"

Firmware application:
  $env:USERPROFILE\onomondo-softsim-test\applications\micro_simulator

Windows relay tools:
  $env:USERPROFILE\onomondo-softsim-test\tools\micro_relay

Short build path:
  C:\b\micro_sim_relay

CURRENT IMPLEMENTATION
----------------------
- Existing Version 7 application envelope remains unchanged.
- Dynamic heartbeat packets append a configured trusted-device count and four
  fixed six-byte slots. Unused slots are zero-filled.
- Original Version 7 heartbeat packets remain decodable.
- Static beacon addresses remain separate from the trusted-device registry.
- Settings updates use command 0x20 and schema-versioned TLV entries.
- Persistent settings include heartbeat interval, LTE update interval, sleep
  interval, safe zones, beacons, and trusted devices.
- Serial commands include config show, generate, apply, set, reset, and last.
- TCP server AUTO mode returns OK, ERROR, or SUP plus one binary settings
  packet. FWUP is recognized and safely deferred.
- Pending settings are associated with a target IMEI and become
  sent_unconfirmed after transmission. No final device acknowledgement is
  invented.
- The server and relay keep local protocol/connection logs. Traffic is not
  forwarded to a second diagnostic server.

TESTS
-----
From tools\protocol_test:
  python3 -m py_compile *.py ..\micro_relay\micro_serial_wifi_relay.py
  python3 -m unittest -v

Linux/macOS host C parser test:
  ./run_c_settings_tests.sh

The completed package passed 58 Python tests and the host C settings test.
A complete nRF Connect SDK build, board flash, LTE connection, and physical
reboot-persistence test were not run in the delivery environment.

FIRMWARE-PARSER COMPATIBILITY NOTE
----------------------------------
The provided under-development production function
app_settings_apply_update_payload() parses a positional payload containing
safe zones, beacons, trusted devices, LTE and sleep intervals, and a final
00/FF flag. The attached implementation specification explicitly required a
new command 0x20 TLV payload. The package follows that requested TLV design
and uses the production function as a reference for setting meanings and
field encodings. The two implementations are not wire-compatible until the
production firmware adopts command 0x20 and the same TLV schema, or an
explicit compatibility translator is defined.

SECURITY
--------
Do not commit SoftSIM profiles, API keys, RSA private keys, server passwords,
IRKs, bonding keys, or other credentials. Trusted-device packets contain only
the six-byte non-secret identifier/address representation currently defined
for simulator testing.

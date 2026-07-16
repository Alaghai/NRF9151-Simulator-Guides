MICRO SIMULATOR DUAL-TRANSPORT PACKAGE
nRF9151 DK + Onomondo SoftSIM LTE-M + Windows Wi-Fi TCP Relay
Documentation release: Version 4

PURPOSE
-------
This package provides the firmware, Windows relay utility, scripts, and four-document guide series needed to run the Micro serial-controlled simulator on a Nordic nRF9151 DK.

The simulator supports two network paths:

1. Direct LTE TCP
   nRF9151 DK -> provisioned Onomondo SoftSIM -> LTE-M -> TCP server

2. Windows Wi-Fi relay
   nRF9151 DK -> USB serial -> Windows laptop -> Wi-Fi/Internet -> TCP server

Server responses travel back through the same selected path. Relay mode works with or without a provisioned SoftSIM and is intended for countries where LTE-M is unavailable.

PROJECT SOFTWARE STANDARD
-------------------------
Use nRF Connect SDK v3.4.x and the matching v3.4 toolchain. Install both through the nRF Connect for Visual Studio Code extension.

Do not mix SDK and toolchain versions.

FOUR-DOCUMENT SERIES
--------------------
1_Onomondo_Softsim_NRF9151_Setup.docx
    Installs the Nordic environment, creates the Onomondo west workspace, explains SoftSIM security, obtains or generates a board-specific SoftSIM HEX profile, provisions the board, and validates LTE attachment.

2_Simulator_Clean_Build_Setup_Guide.docx
    Installs the package, builds and flashes the dual-transport firmware, explains first boot with and without SoftSIM, creates an isolated Windows Python relay environment, and validates both network paths.

3_Simulator_Protocol_Decisions_and_Usage.docx
    Documents the current packet decisions, placeholder CRC behavior, serial commands, transport selection, presets, test scenarios, relay framing, and bidirectional server-response handling.

4_Simulator_TCP_Server_and_Command_Guide.docx
    Covers server credentials, SSH, systemd, journal logs, packet logs, safe Python-server edits, direct LTE testing, relay testing, custom servers, and response messages sent back to the board.

TERMINAL NAMES USED IN THE GUIDES
---------------------------------
nRF Connect SDK v3.4 terminal
    Open in VS Code from the nRF Connect Welcome view. Use for west, git, build, and flash commands.

nRF Terminal / board serial terminal
    Open from the nRF Connect Connected Devices view. Use for board logs, firmware commands, and SoftSIM profile paste when the Python relay is not running.

Standard Windows PowerShell
    Open from the Windows Start menu. Use for Python relay setup, relay launch scripts, SSH, and normal Windows file commands. Do not install Python packages from the Nordic SDK terminal.

Python relay console
    The window running micro_serial_wifi_relay.py. It owns the board COM port and becomes the board serial terminal during relay operation.

Remote Ubuntu shell
    The shell shown after ssh login to the TCP server. Use for systemctl, journalctl, nano, and Linux server commands.

PACKAGE CONTENTS
----------------
Micro_Simulator_Dual_Transport_Complete_v4\
  applications\micro_simulator\
    CMakeLists.txt
    prj.conf
    sysbuild.conf
    src\main.c
  tools\micro_relay\
    micro_serial_wifi_relay.py
    requirements.txt
    setup_windows_relay.ps1
    run_windows_relay.ps1
    list_serial_ports.ps1
  scripts\
    install_micro_simulator.ps1
  docs\
    1_Onomondo_Softsim_NRF9151_Setup.docx
    2_Simulator_Clean_Build_Setup_Guide.docx
    3_Simulator_Protocol_Decisions_and_Usage.docx
    4_Simulator_TCP_Server_and_Command_Guide.docx
  README.txt
  CHANGELOG.txt
  PACKAGE_MANIFEST.txt

STANDARD PATHS
--------------
The guides avoid hard-coded Windows usernames.

Workspace:
    $env:USERPROFILE\onomondo-softsim-test

Firmware application:
    $env:USERPROFILE\onomondo-softsim-test\applications\micro_simulator

Windows relay tools:
    $env:USERPROFILE\onomondo-softsim-test\tools\micro_relay

SoftSIM CLI/private provisioning folder:
    C:\ss

Short firmware build folder:
    C:\b\micro_sim_relay

STARTING FROM THE BEGINNING
---------------------------
1. Read Document 1.
2. Install nRF Connect for VS Code, nRF Connect SDK v3.4.x, and the matching toolchain.
3. Create the Onomondo SoftSIM west workspace.
4. Provision a SoftSIM profile now, or continue without one for relay-only testing.
5. Read Document 2.
6. Run the package installer.
7. Build and flash the firmware from the nRF Connect SDK v3.4 terminal.
8. Set up the Windows relay from standard Windows PowerShell.
9. Use Document 3 to send simulator packets.
10. Use Document 4 to observe packets and develop server responses.

ASKING AMIR FOR ACCESS
----------------------
Authorized team members can ask Amir for:

- A board-specific Onomondo SoftSIM HEX profile.
- Current TCP server login credentials.
- Current endpoint/account details needed for team testing.

Request this information through a private channel. Do not place passwords, API keys, private keys, or SoftSIM HEX profiles in shared documentation, Git, tickets, or public messages.

Each SoftSIM HEX profile should be used for one physical board only.

INSTALL THE PACKAGE
-------------------
Open standard Windows PowerShell:

    Set-Location C:\tmp\Micro_Simulator_Dual_Transport_Complete_v4\scripts
    powershell -ExecutionPolicy Bypass -File .\install_micro_simulator.ps1

The installer uses the current Windows user's profile folder automatically.

BUILD AND FLASH
---------------
Open the nRF Connect SDK v3.4 terminal in VS Code:

    $ws = Join-Path $env:USERPROFILE "onomondo-softsim-test"
    Set-Location $ws
    Remove-Item Env:ZEPHYR_BASE -ErrorAction SilentlyContinue
    west build --sysbuild -b nrf9151dk/nrf9151/ns -s applications\micro_simulator -d C:\b\micro_sim_relay --pristine=always
    west flash -d C:\b\micro_sim_relay

SET UP THE WINDOWS RELAY
------------------------
Open standard Windows PowerShell:

    $relay = Join-Path $env:USERPROFILE "onomondo-softsim-test\tools\micro_relay"
    powershell -ExecutionPolicy Bypass -File (Join-Path $relay "setup_windows_relay.ps1")

List COM ports:

    powershell -ExecutionPolicy Bypass -File (Join-Path $relay "list_serial_ports.ps1")

Close VS Code nRF Terminal, then start the relay:

    powershell -ExecutionPolicy Bypass -File (Join-Path $relay "run_windows_relay.ps1") -Port COM7

Replace COM7 with the actual nRF9151 DK application-console port.

QUICK RELAY TEST
----------------
Type these commands in the Python relay console:

    transport relay
    connect 137.184.163.176 5000
    mode ascii_hex
    send sample_hb_beacon
    send sample_hb_gps
    send sample_location

QUICK LTE TEST
--------------
Use a board with a provisioned SoftSIM and LTE-M coverage. Type these commands in nRF Terminal or the Python relay console:

    softsim status
    transport lte
    lte connect
    lte status
    connect 137.184.163.176 5000
    mode ascii_hex
    send sample_hb_beacon

PROVISION SOFTSIM FROM THE SIMULATOR
------------------------------------
Authorized users can ask Amir for a board-specific HEX profile.

1. Copy the complete profile to the clipboard.
2. In the console that owns the COM port, run:

       softsim provision

3. Paste the complete HEX profile once.
4. Wait approximately 2.5 seconds after paste activity stops.
5. Confirm provisioning and automatic reboot.
6. After reboot, run softsim status and lte connect.

SERVER VERIFICATION
-------------------
Open standard Windows PowerShell:

    ssh root@137.184.163.176

Then in the remote Ubuntu shell:

    journalctl -u micro-tcp-server -f -n 0

The current server normally returns OK. The Windows relay converts all server responses to serial-safe HEX and sends them back to the board, where the firmware decodes and prints them.

SECURITY
--------
Never commit or distribute:

- Onomondo SoftSIM API keys.
- RSA private keys.
- Board SoftSIM HEX profiles.
- TCP server passwords.
- Other production credentials.

CURRENT PROTOCOL LIMITATIONS
----------------------------
- Dynamic packet CRC is still a placeholder.
- The current dynamic LTE location timestamp uses nine fixed sample bytes; final protocol length remains to be finalized.
- Exact sample commands should be used first for parser bring-up.
- Relay mode validates packet and response behavior but not LTE coverage, modem power, RRC/PSM behavior, or cellular latency.

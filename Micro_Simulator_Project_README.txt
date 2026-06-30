MICRO SIMULATOR PROJECT README
nRF9151 DK + Onomondo SoftSIM + Serial-Controlled TCP Simulator

Purpose
-------
This folder contains the documentation and code needed to get the Micro serial-controlled simulator running on a Nordic nRF9151 DK using Onomondo SoftSIM and a TCP server.

The simulator lets developers send Micro protocol heartbeat packets, LTE-M location update packets, exact sample packets, dynamic test packets, and raw custom HEX packets from the nRF9151 DK over LTE-M to a TCP server.

The full workflow is:

1. Provision Onomondo SoftSIM onto the nRF9151 DK.
2. Install the Micro simulator firmware project into the existing Onomondo SoftSIM workspace.
3. Build and flash the simulator firmware onto the nRF9151 DK.
4. Use the serial terminal to connect to a TCP server and send test packets.
5. Watch server logs to confirm that messages are received and to develop server-to-device response behavior.

The intended hardware/software path is:

Serial terminal commands
    -> nRF9151 DK
        -> Onomondo SoftSIM LTE-M
            -> TCP socket
                -> TCP server


Document Series Overview
------------------------
This is intended to be a four-document series. The documents can be read in order, but each document also has a specific purpose.


Document 1: Onomondo SoftSIM nRF9151 DK Setup
---------------------------------------------

Purpose:

This document explains how to set up Onomondo SoftSIM on the Nordic nRF9151 DK.

It covers:

- Creating or using the Onomondo SoftSIM workspace.
- Initializing the Onomondo SoftSIM repository with west.
- Making sure the onomondo-uicc submodule is initialized.
- Building and flashing the Onomondo external-profile sample.
- Downloading and using the Onomondo SoftSIM CLI.
- Generating the RSA key pair.
- Fetching an encrypted SoftSIM profile from Onomondo.
- Converting the encrypted profile into the HEX profile accepted by the board.
- Pasting the SoftSIM HEX profile into the nRF serial terminal.
- Confirming LTE registration.

Read this first if the board has not already been provisioned with an Onomondo SoftSIM profile.

People who already have a working provisioned board can usually skip this document and start with Document 2.

Important security note:

The Onomondo API key, RSA private key, and generated SoftSIM HEX profile are sensitive. They should not be committed to GitHub, shared in public chats, or included in general project documentation. Keep the C:\ss folder private.


Document 2: Micro Simulator Clean Build Setup Guide
---------------------------------------------------
Purpose:

This document explains how to install, build, and flash the Micro simulator firmware application after the Onomondo SoftSIM workspace already exists.

It assumes the user has already completed the SoftSIM setup from Document 1.

It covers:

- Where the Micro simulator application folder belongs.
- How to avoid Windows path-length issues by using a short build directory.
- How to use the clean build package or GitHub/email-delivered code.
- How to run the PowerShell setup script.
- How to verify that main.c contains C code and not documentation.
- How to build with west using the nRF9151 DK non-secure board target.
- How to flash the application.
- How to handle the SoftSIM profile prompt if the board is not already provisioned.
- How to suppress unnecessary SoftSIM debug logging while keeping useful simulator logs.

Main workspace path used in the current project:

C:\Users\INSERTUSER\onomondo-softsim-test

Application path:

C:\Users\INSERTUSER\onomondo-softsim-test\applications\micro_simulator

Short build path:

C:\b\micro_sim_clean

Board target:

nrf9151dk/nrf9151/ns

Primary build command:

west build --sysbuild -b nrf9151dk/nrf9151/ns -s applications\micro_simulator -d C:\b\micro_sim_clean --pristine=always

Primary flash command:

west flash -d C:\b\micro_sim_clean


Document 3: Micro Simulator Protocol Decisions and Serial Command Usage
-----------------------------------------------------------------------
Recommended file names:

micro_simulator_protocol_decisions_and_usage_v3.docx
or
micro_simulator_serial_documentation_sheet.docx

Purpose:

This document explains how to use the simulator after the firmware has been flashed and LTE is connected.

It covers:

- The simulator operating model.
- Default simulator values.
- Packet and field meanings.
- Heartbeat packets.
- LTE-M location update packets.
- Exact protocol sample packet commands.
- Dynamic packet generation.
- Preset profiles such as normal, low battery, charging, outside safe-zone, beacon, and trusted-device states.
- Serial CLI commands.
- Field editing commands.
- Copy-paste test sequences.
- Known protocol decisions and assumptions.

Core serial commands include:

help
status
connect
connect <PUBLIC_IPV4> <PORT>
disconnect
recv
mode ascii_hex
mode binary
send sample_hb_beacon
send sample_hb_gps
send sample_location
send hb
send loc
send raw <HEX_STRING>
preset normal
preset low
preset charging
preset outside
preset beacon
preset trusted
set battery low|medium|high
set charging on|off
set opcode beacon|trusted|gps_lte|gps_safezone
set pos <latitude> <longitude> <accuracy_m> <speed_mps>
set imei <15_DIGIT_IMEI>
set lastupdate <minutes>
set versions <software_version> <firmware_version>
set beacon <12_HEX_CHARS>
set trusted <12_HEX_CHARS>

This document is the main user guide for developers testing simulator behavior from the serial terminal.


Document 4: TCP Server and Response Testing Guide
-------------------------------------------------
Recommended file name:

Micro_Simulator_TCP_Server_and_Command_Guide_v2.docx

Purpose:

This document explains how to connect to and operate the TCP server used for validating simulator messages.

It covers:

- How to SSH into the DigitalOcean server.
- How to check whether the TCP server service is running.
- How to watch live logs.
- How to watch saved packet logs.
- How to confirm the server is listening on the expected port.
- How to restart the service after editing.
- How to safely edit the Python TCP server.
- How to test server responses such as OK, CONFIG_NONE, and future configuration update messages.
- How to troubleshoot firewall, port, timeout, and systemd issues.

Important server command examples:

ssh root@<PUBLIC_IPV4_ADDRESS>

systemctl status micro-tcp-server --no-pager

journalctl -u micro-tcp-server -f -n 0

tail -f /root/micro_tcp_packets.log

tail -n 100 /root/micro_tcp_packets.log

python3 -m py_compile /root/micro_tcp_server.py

systemctl restart micro-tcp-server

ss -ltnp | grep ':5000'

ufw status verbose

Current default test server from this project:

Public IPv4: 137.184.163.176
TCP port:    5000
Service:     micro-tcp-server
Server file: /root/micro_tcp_server.py
Packet log:  /root/micro_tcp_packets.log

Some developers may choose to connect the board to their own TCP server instead. That is supported. They only need to use the simulator serial command:

connect <PUBLIC_IPV4> <PORT>

For example:

connect 203.0.113.10 5000

The firmware should use the public IPv4 address or reachable public DNS/IP of the TCP server. Do not use a DigitalOcean private IP unless the device is actually on the same private network, which it is not when using LTE-M.


Code Package Overview
---------------------
The clean build code package contains the firmware application and setup script needed for Document 2.

Recommended code package file:

Micro_Simulator_Clean_Build_Code_Files.zip

Expected folder structure:

micro_simulator_clean_build/
  applications/
    micro_simulator/
      CMakeLists.txt
      prj.conf
      sysbuild.conf
      src/
        main.c
      docs/
        build_setup_guide.md
        serial_commands.md
  scripts/
    create_clean_micro_simulator.ps1

Main code/config files:

CMakeLists.txt
    Adds the Onomondo SoftSIM overlay and builds src/main.c.

prj.conf
    Enables logging, networking, sockets, modem/LTE support, UART interrupt input, reboot support, heap size, and quiet SoftSIM logs.

sysbuild.conf
    Bundles the SoftSIM template partition data into the sysbuild image.

src/main.c
    The Micro simulator firmware. It handles SoftSIM provisioning, LTE connection, serial command parsing, TCP connection, persistent socket operation, packet construction, and packet sending.

scripts/create_clean_micro_simulator.ps1
    Installs the clean simulator application into the existing Onomondo SoftSIM workspace and clears the short build folder.

The code assumes the Onomondo SoftSIM workspace already exists at:

C:\Users\INSERTUSER\onomondo-softsim-test

The setup script does not delete the Onomondo SoftSIM module or the C:\ss provisioning folder. It only replaces the Micro simulator application folder and clears the simulator build directory.


Recommended Reading Order
-------------------------
For a new developer using a blank nRF9151 DK:

1. Read Document 1 and provision Onomondo SoftSIM.
2. Read Document 2 and build/flash the Micro simulator firmware.
3. Read Document 3 and test simulator serial commands.
4. Read Document 4 and watch TCP server logs while testing.

For a developer whose board already has SoftSIM provisioned:

1. Start with Document 2.
2. Use Document 3 for serial simulator commands.
3. Use Document 4 for TCP server verification.

For a backend developer using their own TCP server:

1. Skim Document 2 to understand what firmware is running.
2. Use Document 3 to generate test packets from the board.
3. Use the connect <PUBLIC_IPV4> <PORT> command to point the simulator at their own server.
4. Use Document 4 only as an example of how the project TCP server logs and responds.


Quick Start After Firmware Is Flashed
-------------------------------------
Open the nRF serial terminal.

If the board asks for the SoftSIM HEX profile, paste the generated profile from C:\ss\profile_001.hex.

After LTE connects, run:

help
connect 137.184.163.176 5000
mode ascii_hex
send sample_hb_beacon
send sample_hb_gps
send sample_location
status

Then test dynamic simulator behavior:

preset normal
send hb

preset low
send hb

preset outside
send hb
send loc


Quick TCP Server Verification
-----------------------------
On the DigitalOcean server, run:

journalctl -u micro-tcp-server -f -n 0

In another server window, optionally run:

tail -f /root/micro_tcp_packets.log

Then send packets from the board. The server should show new connection and packet data.


Notes and Cautions
------------------
- Keep Onomondo API keys, private keys, and SoftSIM HEX profiles private.
- Do not put C:\ss into Git.
- Use short Windows build paths such as C:\b\micro_sim_clean.
- If main.c build errors mention Markdown symbols such as #, ##, or backticks, the wrong file was copied into main.c.
- Use ascii_hex mode first because it is easiest to debug in server logs.
- Use binary mode only after the backend parser is ready.
- If connecting to a custom TCP server, make sure the server has a public reachable IP/port and that firewalls allow inbound TCP traffic.
- The simulator is for bring-up and backend testing. Final production firmware may need additional endpoint fallback logic, final CRC behavior, and final server-to-device config update handling.


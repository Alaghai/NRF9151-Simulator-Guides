MICRO SIMULATOR DUAL-TRANSPORT PACKAGE - CANONICAL VERSION 8

PURPOSE
-------
This package simulates a Micro device and a TCP test server. It implements the
canonical Version 7 binary envelope, the mandatory trusted-device registry on
every heartbeat, the Version 8 BLE/GNSS runtime state machine, three
independent timers, and the server-to-device command-0x02 full-replacement
configuration update.

The source-of-truth protocol is:
  docs\Micro_Data_Packet_V8_State_Machine_and_Settings.docx

The Version 8 simulator and server guides are:
  docs\2_Simulator_Protocol_Decisions_and_Usage_v8_RUNTIME_STATE_MACHINE.docx
  docs\3_Simulator_TCP_Server_and_Command_Guide_v8_RUNTIME_STATE_MACHINE.docx

PROTOCOL SUMMARY
----------------
  AB | 10 | LENGTH_BE | CRC_BE | SEQUENCE_BE | COMMAND | PAYLOAD

- Length is Command + Payload; total packet bytes = 8 + Length.
- CRC is CRC-16/XMODEM (initial 0x0000) over Payload only and is stored BE.
- Command 0x01 = heartbeat, 0x02 = configuration update, 0x10 = LTE-M location.
- All multi-byte numeric fields use big-endian byte order.
- Command 0x02 contains Target IMEI, Update ID, all lists, all intervals, and
  SendingUpdate. It is a strict atomic full replacement.
- Command 0x20 TLV is deprecated and rejected by the normal integration path.
- The third interval field in command 0x02 is bleCheckIntervalSeconds. Its
  two-byte big-endian position is unchanged; sleep_interval_seconds is only a
  legacy local CLI alias.
- Firmware starts in configuration-only mode. Use `simulation on` to enable
  local BLE checks, periodic heartbeats, and LTE location updates while outside.

INSTALL
-------
Open Windows PowerShell in this folder and run:

  Set-Location .\scripts
  powershell -ExecutionPolicy Bypass -File .\install_micro_simulator.ps1

BUILD AND FLASH FIRMWARE
------------------------
Open the nRF Connect SDK v3.4 terminal:

  $ws = Join-Path $env:USERPROFILE "onomondo-softsim-test"
  Set-Location $ws
  Remove-Item Env:ZEPHYR_BASE -ErrorAction SilentlyContinue
  west build --sysbuild -b nrf9151dk/nrf9151/ns -s applications\micro_simulator -d C:\b\micro_sim_relay --pristine=always
  west flash -d C:\b\micro_sim_relay

RUN PYTHON TESTS
----------------
In Anaconda Prompt or PowerShell, run:

  Set-Location .\tools\protocol_test
  python -m unittest -v

Input: the simulator/server protocol code and regression vectors.
Output: validation of envelope, CRC, heartbeat registry, configuration packets,
TCP framing, IMEI-isolated queued updates, and response buffering.

RUN THE TCP SERVER
------------------
From tools\protocol_test:

  python micro_tcp_server.py

Input: binary Version 7 packets or diagnostic ASCII-HEX packets on TCP port
5000 by default. Output: OK, ERROR, or SUP followed by a binary command-0x02
configuration packet when a matching IMEI has a queued update.

QUEUE A COMPLETE CONFIGURATION UPDATE
-------------------------------------
From tools\protocol_test:

  python micro_update_tool.py queue --imei 861352064050787 --update-id 1 --set heartbeat_interval_seconds=60 --set lte_update_interval_seconds=1023 --set ble_check_interval_seconds=480 --set safe_zones=02B513BCFB7CF3D00096 --set beacon_list=0FAC91003B91 --set trusted_device_list=AABBCCDDEE01 --set sending_update=00

Every --set value is required because command 0x02 replaces the complete
configuration. Safe-zone records are 10 bytes, and beacon/trusted lists are
six-byte identifiers concatenated as HEX. Use an empty value after '=' to
clear a list.

SERIAL CONSOLE
--------------
Useful firmware commands:

  status
  config show
  config apply <complete_command_0x02_packet_hex>
  config generate <setting_name> <value>
  config set <setting_name> <value>
  config reset confirm
  config last
  simulation status
  simulation on
  simulation off
  set beacon <12_hex_chars>|off
  set trusted <12_hex_chars>|off

config generate and config set build a complete command-0x02 replacement from
the active configuration; config apply validates a pasted server packet through
the same atomic validation and persistence path as TCP delivery. `status` and
a successfully applied configuration packet show every configured zone, beacon,
trusted device, timer setting, and current non-secret runtime state.

BUILD THE WORD DOCUMENTS
------------------------
From Anaconda Prompt or PowerShell:

  Set-Location .\tools\protocol_test
  python build_protocol_docs.py

Input: the canonical packet decisions, state-machine behavior, and regression
vectors embedded in the script. Output: the two existing simulator/server
guide filenames listed in the request. The updated Micro Data Packet V8 source
document is already present beside them. The script requires
python-docx (included in the Codex bundled Python environment used for this
delivery). If using a separate Anaconda environment, install python-docx first:

  conda install python-docx

SECURITY
--------
Do not commit SoftSIM profiles, API keys, private keys, passwords, IRKs, or
bonding keys. The simulator's six-byte beacon and trusted-device identifiers
are non-secret test values only. Firmware-image transfer is out of scope.

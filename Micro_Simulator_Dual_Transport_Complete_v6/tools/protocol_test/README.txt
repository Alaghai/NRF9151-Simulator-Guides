MICRO SIMULATOR PROTOCOL TEST TOOLS - CANONICAL VERSION 8

Purpose
-------
This folder contains the shared Version 7-envelope/Version 8-behavior protocol
implementation, decoder, TCP server, pending-update store, response parser,
update tool, deterministic runtime state-machine model, document builder, and
regression tests. It implements the cross-team canonical command-0x02
positional full-replacement configuration update.

Inputs and outputs
------------------
- Input: Version 7 binary packets or diagnostic ASCII-HEX packet text.
- Output: envelope/CRC validation, decoded state, TCP responses, queued
  IMEI-specific command-0x02 packets, and protocol test results.
- The old command-0x20 TLV settings packet is rejected; it is not an accepted
  fallback path.

Important files
---------------
micro_protocol.py
    Shared packet constants, builders, and strict validators.
micro_packet_decoder.py
    Console decoder for heartbeat, location, and command-0x02 packets.
micro_tcp_server.py
    TCP stream-framing test server with automatic IMEI-isolated updates.
micro_pending_store.py
    Atomic JSON pending-update storage by target IMEI.
micro_update_tool.py
    Generates and queues full-replacement command-0x02 configuration packets.
micro_state_machine.py
    Hardware-free reference model for configuration mode, runtime states, and
    independent BLE, heartbeat, and LTE timer schedules.
micro_response_parser.py
    Buffers fragmented OK/ERROR/SUP/FWUP server responses.
build_protocol_docs.py
    Builds the canonical Word protocol and Version 8 guides.

Run all Python tests
--------------------
In Anaconda Prompt or PowerShell:

  python -m unittest -v

The logging-capacity regression is also included and can be run alone:

  python -m unittest -v test_micro_logging_config

Input: applications/micro_simulator/prj.conf. Output: a pass/fail check that
the deferred logger ring has at least 16384 bytes of burst headroom.

Run the host C settings test
----------------------------
On Linux/macOS, or a shell with a C compiler in PATH:

  ./run_c_settings_tests.sh

The host C test validates the firmware configuration-payload parser and
candidate/atomic-application logic. It does not flash hardware.

Generate a full update without queuing it
-----------------------------------------
All seven fields must be supplied. Values for safe_zones, beacon_list, and
trusted_device_list are concatenated HEX. An empty value clears a list.

  python micro_update_tool.py generate --imei 861352064050787 --update-id 1 --set heartbeat_interval_seconds=60 --set lte_update_interval_seconds=1023 --set ble_check_interval_seconds=480 --set safe_zones=02B513BCFB7CF3D00096 --set beacon_list=0FAC91003B91 --set trusted_device_list=AABBCCDDEE01 --set sending_update=00

Queue a pending update
----------------------

  python micro_update_tool.py queue --imei 861352064050787 --update-id 1 --set heartbeat_interval_seconds=60 --set lte_update_interval_seconds=1023 --set ble_check_interval_seconds=480 --set safe_zones=02B513BCFB7CF3D00096 --set beacon_list=0FAC91003B91 --set trusted_device_list=AABBCCDDEE01 --set sending_update=00

Run the server
--------------

  python micro_tcp_server.py

For a valid heartbeat, AUTO mode returns OK followed by newline when no update
is pending. When a matching update exists, it returns SUP followed by newline
and one full binary command-0x02 packet. The server buffers split TCP reads and
keeps updates isolated by target IMEI.

Decode a packet
---------------

  python micro_packet_decoder.py AB100032EF0E00010238363133353230363430353037383700010102B513BCFB7CF3D00096010FAC91003B9101AABBCCDDEE01003C03FF01E000

Build protocol documents
------------------------

  python build_protocol_docs.py

Outputs are written to ../../docs:
- 2_Simulator_Protocol_Decisions_and_Usage_v7_SETTINGS_EXTENSION.docx
- 3_Simulator_TCP_Server_and_Command_Guide_v7_SETTINGS_EXTENSION.docx

The current Micro Data Packet V8 source document is
docs/Micro_Data_Packet_V8_State_Machine_and_Settings.docx. It is kept as a
separate source-of-truth artifact because it may be open in Word while the two
operational guides are regenerated.

BLE interval name
-----------------
Use ble_check_interval_seconds in new tools and automation. It is the same
uint16 packet field previously named sleep_interval_seconds, so existing
canonical packet HEX stays byte-for-byte unchanged. The builders accept the old
name only as a clearly marked compatibility alias.

Security
--------
Do not place SoftSIM profiles, credentials, passwords, IRKs, bonding keys,
private keys, or other secrets in packet logs, test fixtures, or the pending
JSON store. The six-byte development identifiers are non-secret test values.

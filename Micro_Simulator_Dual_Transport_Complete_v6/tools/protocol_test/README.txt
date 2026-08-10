MICRO SIMULATOR PROTOCOL VERSION 7 - SETTINGS-UPDATE EXTENSION

Purpose
-------
This folder contains the shared Python protocol implementation, decoder, TCP
server, pending-update tool, response parser, examples, and automated tests.
The Version 7 envelope remains unchanged.

Implemented extensions
----------------------
- Dynamic heartbeats append a 25-byte configured trusted-device registry.
- Original Version 7 heartbeats remain decodable.
- SETTINGS_UPDATE uses command 0x20 and a typed TLV payload.
- Server AUTO mode returns OK, ERROR, or SUP followed by one binary update.
- FWUP can be recognized by the simulator but firmware-image handling is not
  implemented.
- Pending updates are stored by IMEI in an atomic JSON file.
- Packet and connection traffic is logged locally; no second diagnostic server
  is used.

Files
-----
micro_protocol.py
    Shared Python constants, settings registry, encoders, and validators.
micro_packet_decoder.py
    Decoder for original/extended heartbeat, location, and settings update.
micro_tcp_server.py
    Persistent stream-framing server with AUTO mode and structured logs.
micro_pending_store.py
    Atomic JSON pending-update storage by IMEI.
micro_update_tool.py
    Generate, queue, list, inspect, cancel, remove, and requeue updates.
micro_response_parser.py
    Host-side model/test utility for fragmented OK/ERROR/SUP/FWUP responses.
test_micro_protocol.py
    Heartbeat, settings packet, and C/Python constant-regression tests.
test_micro_packet_decoder.py
    Decoder regression tests.
test_micro_tcp_server.py
    TCP stream, AUTO response, IMEI isolation, and response-fragment tests.
test_micro_settings_host.c / run_c_settings_tests.sh
    Host compilation test for the C settings parser and atomic candidate logic.

Run all Python tests
--------------------
python3 -m unittest -v

Run the host C settings tests
-----------------------------
./run_c_settings_tests.sh

Generate a packet without queuing
---------------------------------
python3 micro_update_tool.py generate \
  --imei 861352064050787 \
  --set heartbeat_interval_seconds=60

Queue a pending update
----------------------
python3 micro_update_tool.py queue \
  --imei 861352064050787 \
  --set heartbeat_interval_seconds=60

List updates
------------
python3 micro_update_tool.py list

Run the server
--------------
Keep all Python files in this directory together. On the deployment server,
copy them to /root, set /root/micro_response_mode.txt to AUTO, and run:

python3 -u /root/micro_tcp_server.py

Important protocol values
-------------------------
- Header: 0xAB
- Property: 0x10
- Length: uint16 big-endian, Command + Payload
- CRC: CRC-16/XMODEM over Payload only, big-endian
- Sequence ID: uint16 big-endian
- Heartbeat: command 0x01
- Location: command 0x10
- Settings update: command 0x20
- Extended address heartbeat: 70 bytes, Length 0x003E
- Extended GPS heartbeat: 76 bytes, Length 0x0044

Firmware-parser compatibility note
----------------------------------
The supplied under-development firmware function parses a positional update
payload containing zones, beacons, trusted devices, LTE interval, sleep
interval, and a final sending-update flag. The simulator task specification
explicitly requested the new TLV command 0x20. The setting meanings were used
as reference, but the TLV packet is not binary-compatible with that positional
firmware parser until the firmware implements the same command and schema.

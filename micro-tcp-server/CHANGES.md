# Micro V8 server changes

## Operator login

- The optional `microoperator` login uses its own SSH password and scoped service permissions. Its `microctl` command can read the audit, queue validated V8 settings packets, and export a SQLite snapshot.

## Service startup

- systemd waits for the TCP listener and SQLite initialization before returning from service startup. This lets the installer's immediate smoke test reach the listener reliably.

## DigitalOcean redeployment

- The server delivers one raw binary V8 `0x02` response directly to a valid matching heartbeat. The previous listener sent `SUP\n` before the packet. V8 packet fields, byte order, length and CRC follow the supplied protocol documents.
- The deployment uses an unprivileged systemd service and a root-owned install directory. The new endpoint is configured when setting up the Droplet and when connecting the board or Windows relay.
- SQLite replaces the pending-update JSON and log files. It stores every accepted connection’s TCP chunks, complete packet validation, claimed IMEIs, planned and completed responses, and queue history. Point the server and the CLI at the same database. The CLI can import a saved V8 JSON queue and take a consistent live database backup.
- Concurrent heartbeats claim the current update atomically. New queue entries preserve prior history. After a successful socket write the record is `sent_unconfirmed`. Interrupted writes become `delivery_unknown` at startup, and send failures and uncertain delivery require an operator to requeue.
- `queue` and `generate` take all seven configuration fields. An empty list clears that list. `devices`, `messages`, `history`, and `backup` support day-to-day operation. The deployed listener always validates and applies the documented AUTO response logic. Legacy diagnostic response files and manual reply modes belong to the previous development listener.
- The Word deployment companion supersedes the old endpoint, `/root` layout, log paths and partial `--set` example in the TCP server guide. The protocol decisions and data packet specifications remain the wire references.

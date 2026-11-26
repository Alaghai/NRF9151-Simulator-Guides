# Micro V8 TCP server

This service receives canonical Micro V8 heartbeats and LTE-M location packets from the nRF9151 simulator over TCP. It stores incoming bytes, packet validation and parsed fields, the claimed IMEI when available, server replies, and update history in SQLite. Use the operator CLI to stage a full settings replacement for an IMEI. The service delivers it on that IMEI’s next valid heartbeat.

The [Word deployment guide](Micro%20TCP%20Server%20V8%20Deployment%20Guide.docx) is the current DigitalOcean companion to **Simulator TCP Server and Command Guide V8**. Use this procedure for the new endpoint, service install, operator commands, and logs. The existing V8 protocol and state-machine documents define the wire format. [CHANGES.md](CHANGES.md) explains how this package differs from the previous server.

## Inputs and outputs

| Direction | Input | Output |
| --- | --- | --- |
| Simulator to server | V8 binary heartbeat `0x01` or LTE-M location `0x10`. Diagnostic ASCII-HEX is accepted. | One `OK\n` for a valid location or a heartbeat with no queued update. Otherwise, the matching heartbeat receives one complete raw binary V8 `0x02` packet beginning `AB 10`. |
| Invalid framed input | Bad CRC, bad payload, unknown command, inbound `0x02`, or an IMEI disallowed by local configuration. | `ERROR:INVALID_PACKET\n`. Unframeable junk or a truncated stream is recorded as a framing error or incomplete packet. |
| Operator to server | A 15-digit ASCII IMEI and all seven configuration fields, or a validated prebuilt V8 settings packet. | A durable queue record, sent only in response to a valid matching heartbeat. History records superseded and removed updates. |
| Server to storage | TCP connection events, raw receive chunks, framed packets, validation, response bytes and transmission results. | SQLite `micro.sqlite3`. Framed packets are searchable by IMEI. Raw chunks without a packet-level identity can be retrieved by their connection ID. |

`sent_unconfirmed` records a successful socket write. Confirm device application with `config last` on the simulator. The server keeps one current full settings replacement per IMEI. Queueing a newer packet replaces the previous current candidate and retains its history. Send errors are marked `failed`. A restart resolves an interrupted `sending` record to `delivery_unknown`. After checking the device, an operator can explicitly requeue either one. There is no V8 operation to change an IMEI remotely. The simulator sets its own IMEI and the queue addresses that identity.

## 1. Run on Windows first

The server and CLI use Python 3.12+ and the standard library. Run commands from the `micro-tcp-server` folder. This sample IMEI is for lab testing.

**Anaconda Prompt**

```bat
conda create -n micro-tcp python=3.12 -y
conda activate micro-tcp
cd /d "C:\path\to\micro-tcp-server"
python -m unittest discover -v
python micro_tcp_server.py
```

**PowerShell without Anaconda** if Python 3.12 is installed through the Windows Python launcher

```powershell
Set-Location 'C:\path\to\micro-tcp-server'
py -3.12 -m unittest discover -v
py -3.12 micro_tcp_server.py
```

For the standalone cross-language constant check, set `MICRO_SIMULATOR_SOURCE` to the simulator bundle directory before running the tests. For example, in PowerShell use `$env:MICRO_SIMULATOR_SOURCE = 'C:\path\to\Micro_Simulator_Dual_Transport_Complete_v6'`. The repository layout also finds the bundle automatically.

Leave the first terminal running. Open a second terminal in the same folder. Activate the Anaconda environment again or replace `python` below with `py -3.12` in PowerShell.

```text
python micro_smoke_test.py --host 127.0.0.1 --port 5000 --imei 490154203237518
python micro_update_tool.py devices
python micro_update_tool.py messages --imei 490154203237518 --limit 20
```

The first smoke check expects `OK`. The local server binds `127.0.0.1:5000` and creates `micro-data/micro.sqlite3`. Use one running server per database. Stop it with `Ctrl+C` when you are finished.

## 2. Create the DigitalOcean Droplet

For a low-volume simulator, select **Toronto (TOR1), Ubuntu 24.04 LTS x64, Basic / Regular, 1 vCPU, 1 GiB RAM and 25 GiB SSD**, listed at **USD $6/month**. For more log capacity and memory headroom, use the **2 GiB / 50 GiB** option at **USD $12/month**. Daily backups add 30% under the percentage-based plan. Verify the price in the checkout summary. [Droplet pricing](https://www.digitalocean.com/pricing/droplets) · [Regional availability](https://docs.digitalocean.com/platform/regional-availability/) · [Backup pricing](https://docs.digitalocean.com/products/backups/details/pricing/)

1. In the DigitalOcean control panel, select **Create → Droplets**. Choose the region, operating system, and size above. Select **Bundled** if the configuration selector appears. Choose your existing project, one Droplet, and a name such as `medicalert-micro-v8`. Enable monitoring and choose a backup plan. [DigitalOcean creation steps](https://docs.digitalocean.com/products/droplets/how-to/create/)
2. Choose **SSH Key** authentication. Select your existing key or run `ssh-keygen -t ed25519` in Windows PowerShell and upload the `.pub` file. After creation, copy the new public IPv4 address. Verify the host-key fingerprint in the Droplet console with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` before accepting it on first login. [DigitalOcean setup guide](https://docs.digitalocean.com/products/droplets/getting-started/recommended-droplet-setup/)
3. Under **Networking → Firewalls**, create a firewall and attach it to the Droplet. Allow inbound **TCP 22** from your administrator public IP (`/32` for one IPv4). For direct LTE or a direct Windows relay, allow inbound **TCP 5000** only from the approved integration source CIDRs, such as your modem provider’s APN egress addresses or the relay’s public IP. Keep the default outbound rules. When testing through an SSH tunnel only, leave inbound TCP 5000 closed. Check that another attached firewall or UFW rule does not change the intended access. [Cloud Firewall setup](https://docs.digitalocean.com/products/networking/firewalls/how-to/create/) · [Firewall rules](https://docs.digitalocean.com/products/networking/firewalls/how-to/configure-rules/)

Use lab data on this raw TCP endpoint. V8 carries its device identity and location data in plaintext. An IMEI allowlist filters the claimed packet value. Network source restrictions or a private connection establish where clients can reach the service. Admin commands run over SSH.

If you need data from the old Droplet, check **Backups & Snapshots** and any exported archives. A retained backup or snapshot can be used to create a separate recovery Droplet. [Deleted Droplet recovery](https://docs.digitalocean.com/support/how-do-i-recover-a-deleted-droplet/)

## 3. Install on Ubuntu

**Windows PowerShell**

```powershell
$DropletIP = Read-Host 'Enter the new Droplet public IPv4 address'
$Key = Join-Path $env:USERPROFILE '.ssh/id_ed25519'
ssh -i $Key "root@$DropletIP"
```

**Remote Ubuntu shell**, first login as root

```bash
adduser microadmin
usermod -aG sudo microadmin
install -d -m 0700 -o microadmin -g microadmin /home/microadmin/.ssh
install -m 0600 -o microadmin -g microadmin /root/.ssh/authorized_keys /home/microadmin/.ssh/authorized_keys
```

Enter a unique local password for `microadmin` for `sudo`. Open a second PowerShell window, set `$DropletIP` and `$Key` again, and verify `ssh -i $Key "microadmin@$DropletIP"` and `sudo -v` before closing the root session. Set `$Key` to the actual private key path if you used a custom name. Then exit the administrator SSH session to upload the deployable files from PowerShell.

```powershell
Set-Location 'C:\path\to\micro-tcp-server'
$AppFiles = @(Get-ChildItem -File -Filter 'micro_*.py' | Select-Object -ExpandProperty Name)
$Archive = Join-Path $env:TEMP 'micro-tcp-server.tar.gz'
tar.exe -czf $Archive @AppFiles deploy
if ($LASTEXITCODE) { throw 'Archive creation failed' }
scp -i $Key $Archive "microadmin@${DropletIP}:~/micro-tcp-server.tar.gz"
ssh -i $Key "microadmin@$DropletIP"
```

**Remote Ubuntu shell** as `microadmin`

```bash
sudo apt update
sudo apt install -y python3
mkdir -p ~/micro-tcp-server
tar -xzf ~/micro-tcp-server.tar.gz -C ~/micro-tcp-server
cd ~/micro-tcp-server
sudo bash deploy/install.sh
sudo systemctl status micro-tcp-server --no-pager
sudo ss -ltnp 'sport = :5000'
python3 micro_smoke_test.py --host 127.0.0.1 --port 5000 --imei 490154203237518
sudo journalctl -u micro-tcp-server -n 50 --no-pager
```

The installer enables and starts the service as `microtcp`. It waits for the TCP listener and SQLite initialization before reporting the service active. Application files go to `/opt/micro-tcp-server`, configuration to `/etc/micro-tcp-server.env`, and SQLite to `/var/lib/micro-tcp-server/micro.sqlite3`. Repeat the upload and install for updates. It preserves the service environment and database.

Deployment listens on `0.0.0.0:5000`. For tunnel-only use, edit `/etc/micro-tcp-server.env` with `sudoedit` and set `MICRO_SERVER_HOST=127.0.0.1`. You can also set `MICRO_ALLOWED_IMEIS=490154203237518` or use a comma-separated list. Restart with `sudo systemctl restart micro-tcp-server` and use a permitted IMEI in smoke checks. The service allows 32 simultaneous connections, uses TCP keepalive, and gives a partial frame 30 seconds to complete. The idle limit is 24 hours, which accommodates the maximum V8 interval of 65,535 seconds.

### Set up the password operator login

After installing the server, an administrator can create a `microoperator` login for operating the TCP service. From the project directory on Ubuntu, run:

```bash
sudo bash deploy/create-password-operator.sh
```

Enter and confirm the password at the Ubuntu prompt. From another computer, sign in with `ssh microoperator@DROPLET_IPV4` and enter that password. Share the SSH host, username, and verified host-key fingerprint. Pass the password through a private channel. Everyone using this login has the same operator permissions.

The login can view the Micro journal, check or restart the TCP service, run an SSH tunnel to `127.0.0.1:5000`, inspect packets, manage settings, and save a SQLite snapshot. An administrator can run the setup command again to change the password. Root SSH authentication and the listener address stay as configured on the host.

```bash
microctl devices
microctl messages --limit 20
sudo systemctl status micro-tcp-server --no-pager
sudo journalctl -u micro-tcp-server -f --no-pager
```

## 4. Connect the simulator

Run the server-side smoke check first. For a direct connection, from an approved Windows source, `Test-NetConnection -ComputerName $DropletIP -Port 5000` checks the network and `python micro_smoke_test.py --host $DropletIP --port 5000 --imei 490154203237518` checks the protocol. Use the IMEI configured for that lab check. A queued update for that IMEI will be consumed by the smoke heartbeat.

**Direct LTE** in the nRF terminal. Replace `<DROPLET_IPV4>` with the new address and the test digits with the intended simulator IMEI when appropriate.

```text
transport lte
lte connect
lte status
mode binary
set imei 490154203237518
connect <DROPLET_IPV4> 5000
send hb
send loc
config last
```

**Windows relay**. Run this from the simulator bundle with the other serial terminal closed. Replace `COM5` with the board’s actual COM port. Run setup once.

```powershell
Set-Location 'C:\path\to\Micro_Simulator_Dual_Transport_Complete_v6\tools\micro_relay'
.\list_serial_ports.ps1
.\setup_windows_relay.ps1
.\run_windows_relay.ps1 -Port COM5 -ServerHost $DropletIP -TcpPort 5000
```

In the relay terminal, enter `transport relay`, `mode binary`, `set imei 490154203237518`, `connect <DROPLET_IPV4> 5000`, and `send hb`. When using an SSH tunnel, open another PowerShell window with `ssh -i $Key -N -o ExitOnForwardFailure=yes -L 15000:127.0.0.1:5000 "microadmin@$DropletIP"`. Set the relay’s `-ServerHost` to `127.0.0.1` and `-TcpPort` to `15000`, then issue `connect 127.0.0.1 15000` in the relay console. For a tunnel smoke test use `--host 127.0.0.1 --port 15000`.

## 5. Queue a settings update and verify it

`microctl` is installed by the password operator setup. An Ubuntu administrator can also define the shortcut below to use the original administrative CLI. For local Windows use `python micro_update_tool.py` or `py -3.12 micro_update_tool.py` in place of `microctl`. You can paste the single-line `queue` command into Anaconda Prompt, PowerShell, or Ubuntu after substituting the appropriate prefix.

```bash
microctl() {
  sudo -u microtcp /usr/bin/python3 /opt/micro-tcp-server/micro_update_tool.py --store /var/lib/micro-tcp-server/micro.sqlite3 "$@"
}
```

```text
microctl queue --imei 490154203237518 --update-id 42 --set heartbeat_interval_seconds=60 --set lte_update_interval_seconds=1023 --set ble_check_interval_seconds=480 --set safe_zones= --set beacon_list= --set trusted_device_list= --set sending_update=00
microctl inspect --imei 490154203237518
microctl list
```

**The three empty list values clear all existing entries.** Use the actual desired settings for the target device. The interval fields are seconds, from 1 to 65,535. List values are concatenated HEX for up to four entries each. Each safe zone contains signed latitude and longitude in microdegrees and an unsigned radius in meters (10 bytes total, big-endian). Beacon and trusted device IDs are 6 bytes each. Both lists of identifiers and the safe-zone list are full replacements. `sending_update=00` is the ordinary settings example. `FF` signals the firmware-update indicator. `generate` accepts the same fields and prints the packet without queueing it. An already built full packet can be entered with `queue-raw --imei <IMEI> --hex <HEX>`.

Send `send hb` from the real simulator, then run `config last` and `config show` to verify the applied settings and update ID. To verify delivery with a synthetic client instead, use:

```bash
python3 /opt/micro-tcp-server/micro_smoke_test.py --host 127.0.0.1 --port 5000 --imei 490154203237518 --expect update --update-id 42
microctl history --imei 490154203237518
microctl messages --imei 490154203237518 --limit 20
```

To cancel a pending update, use `microctl cancel --imei 490154203237518`. To send the current record again after reviewing its status, use `microctl requeue --imei 490154203237518`. To remove the current record while retaining history, use `microctl remove --imei 490154203237518`.

## 6. Inspect messages and back up the data

```bash
microctl devices
microctl messages --imei 490154203237518 --kind packet --limit 20
microctl messages --connection CONNECTION_ID --limit 100
sudo journalctl -u micro-tcp-server -f --no-pager
df -h /var/lib/micro-tcp-server
```

The CLI prints JSON. `packet` rows hold normalized application bytes and decoded fields. `raw_chunk` rows hold exact received bytes. `response_attempt` holds the intended reply bytes before the socket write. The paired `response_sent` or `response_failed` gives its observed result. The service journal reports lifecycle and operational errors. Keep an eye on the data volume because the message audit retains all rows and local backup copies also take disk space.

To export a consistent live SQLite backup as `microoperator`, run:

```bash
(umask 077; microctl backup > "$HOME/micro-$(date -u +%Y%m%dT%H%M%SZ).sqlite3")
```

To export with the original administrative CLI, use the administrator shortcut in step 5 and run the following in the Ubuntu shell. Store backup files on restricted storage with the same access controls as the live data.

```bash
sudo install -d -m 0700 -o microtcp -g microtcp /var/lib/micro-tcp-server/backups
BackupName="micro-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
microctl backup --out "/var/lib/micro-tcp-server/backups/$BackupName"
sudo install -m 0600 -o "$USER" -g "$(id -gn)" "/var/lib/micro-tcp-server/backups/$BackupName" "$HOME/$BackupName"
printf '%s\n' "$BackupName"
```

In Windows PowerShell, run `$BackupName = Read-Host 'Enter the filename printed by Ubuntu'`, then `scp "microadmin@${DropletIP}:~/$BackupName" .`. Use that database to audit or test recovery from an isolated copy. A live SQLite database may have WAL sidecars. The `backup` command snapshots its consistent state.

If you recovered the old JSON queue, copy it to a private path readable by `microtcp` and run `microctl import-legacy --from /var/lib/micro-tcp-server/legacy-pending.json`. This validates packets and keeps the old delivery statuses. Existing IMEIs in the destination are skipped. Review imported `pending` entries before opening device access. Archive any old text and JSONL logs separately.

## Common connection checks

| Symptom | Check |
| --- | --- |
| TCP probe times out | Confirm the new IP, firewall attachment and source CIDR. Check UFW and confirm the process listens on the desired interface with `ss`. For LTE, verify APN egress with your carrier. |
| Connection works and no message is decoded | Check `messages --connection ...` for raw chunks or incomplete frames. Confirm `mode binary`, a canonical V8 firmware build, and the `AB 10` header and declared length. |
| Heartbeat gets `OK` but settings were expected | Check the exact IMEI, `list` and `history`. Verify the packet is `pending` and the incoming command is a valid heartbeat. A location message receives `OK` without consuming an update. |
| Settings are `sent_unconfirmed` but absent on the simulator | Check `config last` for a validation result and `config show` for active values. Verify the intended target IMEI and update ID before explicitly requeueing. |
| Relay reports a busy COM port | Close nRF Terminal or any other application holding that serial port and start the relay again. Always pass the new endpoint into the relay command. |

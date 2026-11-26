#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    printf 'Run this installer with sudo bash deploy/install.sh.\n' >&2
    exit 1
fi

deploy_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(dirname -- "$deploy_dir")
service_user=microtcp
app_dir=/opt/micro-tcp-server
state_dir=/var/lib/micro-tcp-server
env_file=/etc/micro-tcp-server.env
unit=micro-tcp-server.service

if ! /usr/bin/python3 -c 'import sqlite3, sys; sys.exit(sys.version_info < (3, 12))'; then
    printf 'Install Python 3.12 or later with SQLite support before installing the server.\n' >&2
    exit 1
fi
if [[ ! -d /run/systemd/system ]]; then
    printf 'Run this installer on a Linux host running systemd.\n' >&2
    exit 1
fi

for required_file in "$project_dir/micro_tcp_server.py" "$deploy_dir/$unit" "$deploy_dir/micro-tcp-server.env.example"; do
    if [[ ! -f $required_file ]]; then
        printf 'Deployment file missing: %s\n' "$required_file" >&2
        exit 1
    fi
done

if ! getent passwd "$service_user" >/dev/null; then
    if ! getent group "$service_user" >/dev/null; then
        groupadd --system "$service_user"
    fi
    useradd --system --gid "$service_user" --home-dir "$state_dir" --shell /usr/sbin/nologin "$service_user"
fi
if [[ $(id -gn "$service_user") != "$service_user" || $(id -u "$service_user") -eq 0 ]]; then
    printf 'The existing %s account must be unprivileged and use the %s primary group.\n' "$service_user" "$service_user" >&2
    exit 1
fi

install -d -m 0755 -o root -g root "$app_dir"
install -d -m 0700 -o "$service_user" -g "$service_user" "$state_dir"
for source_file in "$project_dir"/micro_*.py; do
    destination="$app_dir/$(basename -- "$source_file")"
    # Skip files if reinstalling from the application directory itself.
    if [[ ! $source_file -ef $destination ]]; then
        install -m 0644 -o root -g root "$source_file" "$destination"
    fi
done
if [[ ! -e $env_file && ! -L $env_file ]]; then
    install -m 0600 -o root -g root "$deploy_dir/micro-tcp-server.env.example" "$env_file"
fi
install -m 0644 -o root -g root "$deploy_dir/$unit" "/etc/systemd/system/$unit"

systemctl daemon-reload
systemctl enable "$unit"
systemctl restart "$unit"
systemctl is-active "$unit"

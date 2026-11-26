#!/usr/bin/env bash
set -euo pipefail

operator=microoperator
wrapper=/usr/local/bin/microctl
marker_dir=/var/lib/micro-tcp-operator
marker="$marker_dir/$operator"
ssh_drop_in=/etc/ssh/sshd_config.d/00-micro-tcp-operator.conf
sudo_drop_in=/etc/sudoers.d/micro-tcp-operator
managed='# Managed by create-password-operator.sh (Micro V8 TCP).'
app_dir=/opt/micro-tcp-server
state_dir=/var/lib/micro-tcp-server
connection='host=localhost,addr=127.0.0.1,laddr=127.0.0.1,lport=22'
deploy_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

die() {
    printf '%s\n' "$*" >&2
    exit 1
}

[[ $EUID -eq 0 ]] || die 'Run this script as root or with sudo.'
[[ -t 0 && -t 1 ]] || die 'Run this script in an interactive terminal for the password prompt.'
[[ -x /usr/sbin/sshd && -x /usr/sbin/visudo && -x /usr/bin/sudo ]] || die 'Install the Ubuntu OpenSSH server and sudo first.'
[[ -d /run/systemd/system && -d ${ssh_drop_in%/*} && -d ${sudo_drop_in%/*} ]] || die 'This setup uses Ubuntu systemd, OpenSSH drop-ins, and sudoers drop-ins.'
[[ -r "$deploy_dir/microctl" && -r "$app_dir/micro_update_tool.py" && -f "$state_dir/micro.sqlite3" ]] || die 'Keep the bundled microctl beside this script and install and start the Micro TCP server first.'
id microtcp &>/dev/null || die 'The Micro TCP service account microtcp is missing.'
systemctl cat micro-tcp-server.service >/dev/null || die 'The Micro TCP systemd service is missing.'

umask 077
exec 9>/run/lock/micro-tcp-operator.lock
flock -n 9 || die 'Another operator setup is in progress.'

state_present() {
    [[ -e $1 || -L $1 ]]
}

check_managed_file() {
    local path=$1
    if state_present "$path"; then
        [[ -f $path && ! -L $path && $(stat -c '%u' -- "$path") == 0 ]] &&
            grep -Fxq -- "$managed" "$path" || die "Refusing to replace the existing file $path."
    fi
}

replace_file() {
    local source=$1 target=$2 mode=$3 temporary
    temporary=$(mktemp "${target%/*}/.micro-tcp-operator.XXXXXXXXXX") || return 1
    if install -o root -g root -m "$mode" -- "$source" "$temporary" && mv -fT -- "$temporary" "$target"; then
        return 0
    fi
    rm -f -- "$temporary"
    return 1
}

restore_file() {
    local previous=$1 target=$2 mode=$3
    if [[ -f $previous ]]; then
        replace_file "$previous" "$target" "$mode" || printf 'Could not restore %s.\n' "$target" >&2
    else
        rm -f -- "$target" || printf 'Could not remove %s.\n' "$target" >&2
    fi
}

reload_ssh() {
    if systemctl is-active --quiet ssh.service; then
        systemctl reload ssh.service
    else
        # With socket activation, the next SSH connection starts a fresh daemon.
        systemctl is-active --quiet ssh.socket
    fi
}

if state_present "$marker_dir"; then
    [[ -d $marker_dir && ! -L $marker_dir && $(stat -c '%u:%a' -- "$marker_dir") == '0:700' ]] || die "Invalid operator marker directory $marker_dir."
fi
if entry=$(getent passwd "$operator"); then
    IFS=: read -r username _ uid gid _ home shell <<< "$entry"
    [[ $username == "$operator" && $uid != 0 && $home == "/home/$operator" && $shell == /bin/bash && $(id -nG "$operator") == "$operator" ]] &&
        [[ -f $marker && ! -L $marker && $(stat -c '%u:%a' -- "$marker") == '0:600' && $(<"$marker") == "$uid:$gid" ]] ||
        die "The account $operator already exists and does not match the managed operator."
    check_managed_file "$wrapper"
    check_managed_file "$ssh_drop_in"
    check_managed_file "$sudo_drop_in"
else
    for path in "$marker" "$wrapper" "$ssh_drop_in" "$sudo_drop_in" "/home/$operator"; do
        if state_present "$path"; then
            die "Refusing to replace existing operator state at $path."
        fi
    done
fi

if ! systemctl is-active --quiet ssh.service && ! systemctl is-active --quiet ssh.socket; then
    die 'The OpenSSH service or socket must already be active.'
fi
/usr/sbin/sshd -t
root_before=$(/usr/sbin/sshd -T -C "user=root,$connection")

stage=$(mktemp -d /run/micro-tcp-operator.XXXXXXXXXX)
ssh_changed=0
sudo_changed=0
wrapper_changed=0
reload_attempted=0
completed=0

cleanup() {
    local status=$?
    trap - EXIT
    if ((status != 0 && ! completed)); then
        if ((sudo_changed)); then
            restore_file "$stage/sudoers.previous" "$sudo_drop_in" 0440
        fi
        if ((wrapper_changed)); then
            restore_file "$stage/wrapper.previous" "$wrapper" 0755
        fi
        if ((ssh_changed)); then
            restore_file "$stage/sshd.previous" "$ssh_drop_in" 0644
        fi
        if ((reload_attempted)); then
            if ! /usr/sbin/sshd -t || ! reload_ssh; then
                printf 'Could not reload the restored SSH configuration. Check it in the server console.\n' >&2
            fi
        fi
    fi
    rm -f -- "$stage/sshd.previous" "$stage/sshd.next" "$stage/sudoers.previous" \
        "$stage/sudoers.next" "$stage/wrapper.previous" "$stage/marker"
    rmdir -- "$stage"
    exit "$status"
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

cat > "$stage/sudoers.next" <<'EOF'
# Managed by create-password-operator.sh (Micro V8 TCP).
microoperator ALL=(microtcp) NOPASSWD: NOSETENV: /usr/local/bin/microctl
microoperator ALL=(root) NOPASSWD: NOSETENV: /usr/bin/systemctl status micro-tcp-server --no-pager, \
    /usr/bin/systemctl restart micro-tcp-server, \
    /usr/bin/journalctl -u micro-tcp-server -n 50 --no-pager, \
    /usr/bin/journalctl -u micro-tcp-server -f --no-pager
EOF
/usr/sbin/visudo -c -f "$stage/sudoers.next"

cat > "$stage/sshd.next" <<'EOF'
# Managed by create-password-operator.sh (Micro V8 TCP).
Match User microoperator
    AuthenticationMethods password
    PasswordAuthentication yes
    PermitEmptyPasswords no
    KbdInteractiveAuthentication no
    AllowTcpForwarding local
    PermitOpen 127.0.0.1:5000
    AllowAgentForwarding no
    AllowStreamLocalForwarding no
    X11Forwarding no
EOF
if [[ -f $ssh_drop_in ]]; then
    cp -- "$ssh_drop_in" "$stage/sshd.previous"
fi
ssh_changed=1
replace_file "$stage/sshd.next" "$ssh_drop_in" 0644
/usr/sbin/sshd -t
root_after=$(/usr/sbin/sshd -T -C "user=root,$connection")
[[ $root_before == "$root_after" ]] || die 'The proposed configuration changed the effective root SSH settings.'

if [[ -z $entry ]]; then
    useradd --create-home --home-dir "/home/$operator" --user-group --shell /bin/bash -- "$operator"
    install -d -o root -g root -m 0700 -- "$marker_dir"
    printf '%s:%s\n' "$(id -u "$operator")" "$(id -g "$operator")" > "$stage/marker"
    replace_file "$stage/marker" "$marker" 0600
fi

effective=$(/usr/sbin/sshd -T -C "user=$operator,$connection")
for setting in 'authenticationmethods password' 'passwordauthentication yes' 'permitemptypasswords no' \
    'disableforwarding no' 'allowtcpforwarding local' 'permitopen 127.0.0.1:5000'; do
    grep -Fxq -- "$setting" <<< "$effective" || die "The operator SSH settings do not include '$setting'. Check for an existing matching rule or a missing Include."
done
if [[ -f $wrapper ]]; then
    cp -- "$wrapper" "$stage/wrapper.previous"
fi
if [[ -f $sudo_drop_in ]]; then
    cp -- "$sudo_drop_in" "$stage/sudoers.previous"
fi
wrapper_changed=1
replace_file "$deploy_dir/microctl" "$wrapper" 0755
sudo_changed=1
replace_file "$stage/sudoers.next" "$sudo_drop_in" 0440
/usr/sbin/visudo -c
(cd / && runuser -u "$operator" -- "$wrapper" --help >/dev/null) || die 'The operator could not use the installed microctl policy.'
reload_attempted=1
reload_ssh || die 'OpenSSH could not reload the operator configuration.'

printf '\nSet the SSH password for %s at the following prompt.\n' "$operator"
passwd "$operator"
completed=1
printf '\n%s is configured. Verify its login from a separate terminal.\n' "$operator"

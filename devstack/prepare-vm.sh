#!/usr/bin/env bash
# Copyright (c) 2026 TrueNAS
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
# Prepare a dedicated Ubuntu 24.04 or 26.04 VM to run DevStack with the TrueNAS Cinder
# driver as its only volume backend. Run with --help for usage.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
readonly SCRIPT_DIR REPO_ROOT

readonly STACK_USER=stack
readonly STACK_HOME=/opt/stack
readonly DEVSTACK_DIR=$STACK_HOME/devstack
# Where this repository is copied for DevStack to clone as a plugin, and
# where DevStack then clones it (enable_plugin truenas-cinder ...).
readonly PLUGIN_SRC=$STACK_HOME/src/truenas_cinder
readonly PLUGIN_CLONE=$STACK_HOME/truenas-cinder
readonly TEMPLATE=$SCRIPT_DIR/local.conf.example

# Every setting resolve_settings() reads from the environment. When the
# script re-runs itself as root, exactly these are carried across.
readonly SETTINGS_VARS=(
    TRUENAS_IP TRUENAS_URL TRUENAS_API_KEY TRUENAS_LOGIN TRUENAS_USER
    TRUENAS_POOL TRUENAS_PORTAL_ID TRUENAS_DATASET_ROOT TRUENAS_VERIFY_SSL
    TRUENAS_INSECURE_SKIP_VERIFY TRUENAS_MULTIPATH TRUENAS_API_CLIENT_REPO
    TRUENAS_API_CLIENT_REF ADMIN_PASSWORD DEVSTACK_BRANCH HOST_IP LIBVIRT_TYPE
)

DRY_RUN=False
RUN_STACK=False
CLEANUP_DIRS=()

usage() {
    cat <<'EOF'
Prepare a dedicated Ubuntu 24.04 or 26.04 VM to run DevStack with the
TrueNAS Cinder driver as its only volume backend.

Run it from a checkout of this repository on the VM:

  export TRUENAS_IP=192.0.2.10
  export TRUENAS_API_KEY=16-AbCdEf...
  ./devstack/prepare-vm.sh --dry-run   check and preview; changes nothing
  ./devstack/prepare-vm.sh             prepare the VM
  ./devstack/prepare-vm.sh --stack     prepare, then run stack.sh

Run it as yourself, not with sudo: it re-runs itself with sudo, carrying
the settings below across (plain sudo would drop them).

Settings, read from the environment:

  TRUENAS_IP            TrueNAS address (or set TRUENAS_URL)          required
  TRUENAS_API_KEY       TrueNAS API key                               required
  TRUENAS_LOGIN         user that owns the key  (default truenas_admin)
  TRUENAS_POOL          ZFS pool                (default tank)
  TRUENAS_PORTAL_ID     iSCSI portal ID         (default 1)
  TRUENAS_DATASET_ROOT  dataset for volumes     (default devstack)
  TRUENAS_VERIFY_SSL    True or False           (default True, or False when
                                                 TRUENAS_INSECURE_SKIP_VERIFY=true)
  TRUENAS_MULTIPATH     True or False           (default False)
  ADMIN_PASSWORD        DevStack admin password (default: random)
  DEVSTACK_BRANCH       DevStack branch         (default master)
  HOST_IP               DevStack's address      (default: the local address
                                                 that routes to TrueNAS)
  LIBVIRT_TYPE          kvm or qemu             (default: kvm when /dev/kvm
                                                 exists, otherwise qemu)

The names match tools/smoke_test.py, so one set of exports serves both;
TRUENAS_USER is accepted in place of TRUENAS_LOGIN.

The script installs the iSCSI initiator and multipath tools, checks the
TrueNAS settings with tools/smoke_test.py, creates DevStack's "stack" user,
clones DevStack, copies this repository for DevStack to load as a plugin, and
writes /opt/stack/devstack/local.conf from devstack/local.conf.example.
EOF
}

log() { printf '==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    local dir
    for dir in "${CLEANUP_DIRS[@]}"; do
        rm -rf -- "$dir"
    done
}
trap cleanup EXIT

truthy() {
    case "${1,,}" in
    1 | true | yes | y | on) return 0 ;;
    *) return 1 ;;
    esac
}

normalize_bool() {
    if truthy "$1"; then echo True; else echo False; fi
}

as_stack() {
    sudo -u "$STACK_USER" -H -- "$@"
}

# Re-run this script as root, carrying only the settings above. Uses
# --preserve-env=LIST rather than -E because sudo-rs, the sudo on Ubuntu
# 26.04, does not support -E; classic sudo accepts the same option.
become_root() {
    if [[ $EUID -eq 0 ]]; then
        return
    fi
    command -v sudo >/dev/null || die "run this as root (sudo is not installed)"
    local keep
    keep=$(
        IFS=,
        echo "${SETTINGS_VARS[*]}"
    )
    log "Re-running as root with sudo"
    exec sudo --preserve-env="$keep" -- \
        "$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")" "$@"
}

parse_args() {
    local arg
    for arg in "$@"; do
        case $arg in
        --dry-run) DRY_RUN=True ;;
        --stack) RUN_STACK=True ;;
        -h | --help)
            usage
            exit 0
            ;;
        *) die "unknown option: $arg (see --help)" ;;
        esac
    done
}

# The default for a variable as written in devstack/settings, so the API
# client version pinned there is the only one to maintain.
settings_default() {
    local name=$1 line
    line=$(grep -m1 "^${name}=" "$SCRIPT_DIR/settings") ||
        die "devstack/settings has no default for $name"
    line=${line#*:-}
    line=${line%\}}
    if [[ -z "$line" || "$line" == *'$'* || "$line" == *'='* ]]; then
        die "could not read the default for $name from devstack/settings"
    fi
    printf '%s' "$line"
}

# DevStack expands local.conf values through eval, so a value the shell
# would interpret (quotes, $, backticks, spaces) cannot be carried safely.
require_plain() {
    local name=$1
    local value=${!name}
    if [[ ! "$value" =~ ^[A-Za-z0-9._:@/+=-]+$ ]]; then
        die "$name contains characters that cannot be passed through local.conf (allowed: letters, digits and ._:@/+=-)"
    fi
}

resolve_settings() {
    if [[ -z "${TRUENAS_IP:-}" && -n "${TRUENAS_URL:-}" ]]; then
        TRUENAS_IP=${TRUENAS_URL#*://}
        TRUENAS_IP=${TRUENAS_IP%%/*}
    fi
    [[ -n "${TRUENAS_IP:-}" ]] || die "set TRUENAS_IP (or TRUENAS_URL); see --help"
    [[ -n "${TRUENAS_API_KEY:-}" ]] || die "set TRUENAS_API_KEY; see --help"

    TRUENAS_LOGIN=${TRUENAS_LOGIN:-${TRUENAS_USER:-truenas_admin}}
    TRUENAS_POOL=${TRUENAS_POOL:-tank}
    TRUENAS_PORTAL_ID=${TRUENAS_PORTAL_ID:-1}
    TRUENAS_DATASET_ROOT=${TRUENAS_DATASET_ROOT:-devstack}
    if [[ -z "${TRUENAS_VERIFY_SSL:-}" ]]; then
        if truthy "${TRUENAS_INSECURE_SKIP_VERIFY:-}"; then
            TRUENAS_VERIFY_SSL=False
        else
            TRUENAS_VERIFY_SSL=True
        fi
    fi
    TRUENAS_VERIFY_SSL=$(normalize_bool "$TRUENAS_VERIFY_SSL")
    TRUENAS_MULTIPATH=$(normalize_bool "${TRUENAS_MULTIPATH:-False}")
    TRUENAS_API_CLIENT_REPO=${TRUENAS_API_CLIENT_REPO:-$(settings_default TRUENAS_API_CLIENT_REPO)}
    TRUENAS_API_CLIENT_REF=${TRUENAS_API_CLIENT_REF:-$(settings_default TRUENAS_API_CLIENT_REF)}
    if [[ -z "${ADMIN_PASSWORD:-}" ]]; then
        ADMIN_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_hex(12))')
    fi
    DEVSTACK_BRANCH=${DEVSTACK_BRANCH:-master}
    # Detected later unless given.
    HOST_IP=${HOST_IP:-}
    LIBVIRT_TYPE=${LIBVIRT_TYPE:-}

    [[ "$TRUENAS_PORTAL_ID" =~ ^[0-9]+$ ]] ||
        die "TRUENAS_PORTAL_ID must be a number, got '$TRUENAS_PORTAL_ID'"
    [[ -z "$LIBVIRT_TYPE" || "$LIBVIRT_TYPE" == kvm || "$LIBVIRT_TYPE" == qemu ]] ||
        die "LIBVIRT_TYPE must be kvm or qemu, got '$LIBVIRT_TYPE'"
    local name
    for name in TRUENAS_IP TRUENAS_API_KEY TRUENAS_LOGIN TRUENAS_POOL \
        TRUENAS_DATASET_ROOT TRUENAS_API_CLIENT_REF ADMIN_PASSWORD; do
        require_plain "$name"
    done
    if [[ -n "$HOST_IP" ]]; then
        require_plain HOST_IP
    fi
}

preflight_host() {
    log "Checking this machine"
    local os_id='' os_version=''
    if [[ -r /etc/os-release ]]; then
        # shellcheck source=/dev/null
        os_id=$(. /etc/os-release && echo "${ID:-}")
        # shellcheck source=/dev/null
        os_version=$(. /etc/os-release && echo "${VERSION_ID:-}")
    fi
    # The Ubuntu releases in DevStack's SUPPORTED_DISTROS (noble, resolute).
    if [[ "$os_id" != ubuntu || ! "$os_version" =~ ^(24\.04|26\.04)$ ]]; then
        warn "DevStack supports Ubuntu 24.04 and 26.04; this is ${os_id:-unknown} ${os_version}"
    fi
    if grep -qi microsoft /proc/version 2>/dev/null; then
        warn "This looks like WSL. DevStack takes over the machine it runs on; use a distro or VM dedicated to it."
    fi

    local mem_gib cpus disk_gib
    mem_gib=$(awk '/^MemTotal:/ {printf "%d", $2 / 1048576}' /proc/meminfo)
    cpus=$(nproc)
    disk_gib=$(df -BG --output=avail / | tail -n 1 | tr -dc '0-9')
    info "$mem_gib GiB RAM, $cpus CPUs, $disk_gib GiB free on /"
    ((mem_gib >= 8)) || warn "DevStack needs at least 8 GiB of RAM (16 recommended); this has $mem_gib GiB"
    ((cpus >= 4)) || warn "4 or more CPUs recommended; this has $cpus"
    ((disk_gib >= 50)) || warn "50 GiB or more free disk recommended; this has $disk_gib GiB"

    if [[ -z "$LIBVIRT_TYPE" ]]; then
        if [[ -e /dev/kvm ]]; then
            LIBVIRT_TYPE=kvm
        else
            LIBVIRT_TYPE=qemu
            warn "No /dev/kvm, so instances will use slow software emulation. Enable nested virtualization on the hypervisor to use kvm."
        fi
    fi
    info "libvirt type: $LIBVIRT_TYPE"
}

# The local address this VM uses to reach TrueNAS: DevStack's HOST_IP.
host_ip_towards() {
    local address
    address=$(getent ahostsv4 "$1" | awk 'NR == 1 {print $1}') || return 0
    [[ -n "$address" ]] || return 0
    ip -4 route get "$address" 2>/dev/null |
        awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}'
}

preflight_network() {
    log "Checking TrueNAS at $TRUENAS_IP"
    local port
    for port in 443 3260; do
        if timeout 5 bash -c "exec 3<>/dev/tcp/$TRUENAS_IP/$port" 2>/dev/null; then
            info "TCP $port reachable"
        else
            die "cannot reach $TRUENAS_IP on TCP $port"
        fi
    done

    if command -v curl >/dev/null; then
        local versions
        versions=$(curl -fsk --max-time 10 "https://$TRUENAS_IP/api/versions") ||
            die "TrueNAS API did not answer at https://$TRUENAS_IP/api/versions"
        if [[ "$versions" == *'"v25.10.0"'* ]]; then
            info "API version v25.10.0 advertised"
        else
            warn "TrueNAS does not advertise API version v25.10.0 (it advertises: $versions)"
        fi
    else
        warn "curl is not installed; skipping the API version check"
    fi

    if [[ -n "$HOST_IP" ]]; then
        info "using HOST_IP=$HOST_IP from the environment"
        return
    fi
    HOST_IP=$(host_ip_towards "$TRUENAS_IP")
    if [[ -n "$HOST_IP" ]]; then
        info "this machine reaches TrueNAS from $HOST_IP (DevStack HOST_IP)"
    else
        warn "could not work out which local address reaches TrueNAS; DevStack will pick HOST_IP itself"
    fi
}

install_packages() {
    log "Installing the iSCSI initiator, multipath tools, and build prerequisites"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q
    apt-get install -y -q curl git python3 python3-venv sudo \
        open-iscsi multipath-tools
}

preflight_credentials() {
    log "Checking the TrueNAS credentials with tools/smoke_test.py"
    local venv
    venv=$(mktemp -d)
    CLEANUP_DIRS+=("$venv")
    python3 -m venv "$venv"
    "$venv/bin/pip" install --quiet --disable-pip-version-check \
        "truenas-api-client@git+${TRUENAS_API_CLIENT_REPO}@${TRUENAS_API_CLIENT_REF}"

    local insecure=false
    [[ "$TRUENAS_VERIFY_SSL" == True ]] || insecure=true
    if ! TRUENAS_URL="wss://$TRUENAS_IP/api/current" \
        TRUENAS_API_KEY="$TRUENAS_API_KEY" \
        TRUENAS_USER="$TRUENAS_LOGIN" \
        TRUENAS_POOL="$TRUENAS_POOL" \
        TRUENAS_ISCSI_PORTAL="$TRUENAS_PORTAL_ID" \
        TRUENAS_INSECURE_SKIP_VERIFY="$insecure" \
        "$venv/bin/python" "$REPO_ROOT/tools/smoke_test.py"; then
        die "TrueNAS rejected these settings (see the smoke test output above); fix them before running DevStack"
    fi
}

configure_initiator() {
    log "Enabling iscsid and multipathd"
    systemctl enable --now iscsid.socket >/dev/null 2>&1 ||
        systemctl enable --now iscsid >/dev/null
    if [[ -e /etc/multipath.conf ]]; then
        info "keeping the existing /etc/multipath.conf"
    else
        cat >/etc/multipath.conf <<'EOF'
# Written by truenas_cinder devstack/prepare-vm.sh.
defaults {
    user_friendly_names no
    find_multipaths yes
}
EOF
        info "wrote /etc/multipath.conf"
    fi
    systemctl enable multipathd >/dev/null
    systemctl restart multipathd
}

create_stack_user() {
    if id "$STACK_USER" >/dev/null 2>&1; then
        log "User $STACK_USER already exists"
    else
        log "Creating user $STACK_USER with home $STACK_HOME"
        useradd --shell /bin/bash --home-dir "$STACK_HOME" --create-home "$STACK_USER"
    fi
    chmod +x "$STACK_HOME"
    # Validate before installing: a malformed file in sudoers.d can stop
    # sudo working for every user on the machine.
    local rule
    rule=$(mktemp)
    echo "$STACK_USER ALL=(ALL) NOPASSWD: ALL" >"$rule"
    visudo -cqf "$rule" || die "generated sudoers rule failed validation"
    install -m 0440 -o root -g root "$rule" /etc/sudoers.d/stack
    rm -f -- "$rule"
}

clone_devstack() {
    if [[ -d "$DEVSTACK_DIR/.git" ]]; then
        log "DevStack is already cloned at $DEVSTACK_DIR; leaving it as is"
    else
        log "Cloning DevStack ($DEVSTACK_BRANCH) into $DEVSTACK_DIR"
        as_stack git clone --branch "$DEVSTACK_BRANCH" \
            https://opendev.org/openstack/devstack "$DEVSTACK_DIR"
    fi
}

copy_plugin_repo() {
    local from to
    from=$(realpath "$REPO_ROOT")
    to=$(realpath -m "$PLUGIN_SRC")
    if [[ "$from" == "$to" ]]; then
        log "This checkout is already at $PLUGIN_SRC"
    else
        log "Copying this repository to $PLUGIN_SRC"
        rm -rf -- "$PLUGIN_SRC"
        install -d -o "$STACK_USER" -g "$STACK_USER" "$PLUGIN_SRC"
        # Copy the git repository itself, which DevStack clones the plugin
        # from. Copying (rather than cloning) gives the stack user its own
        # repository, which avoids git's refusal to work in one owned by
        # another user.
        tar -C "$REPO_ROOT" --exclude=./.venv --exclude='__pycache__' \
            --exclude=./.pytest_cache --exclude=./.ruff_cache -cf - . |
            tar -C "$PLUGIN_SRC" -xf -
    fi
    chown -R "$STACK_USER:$STACK_USER" "$PLUGIN_SRC"

    TRUENAS_CINDER_BRANCH=$(as_stack git -C "$PLUGIN_SRC" rev-parse --abbrev-ref HEAD)
    [[ "$TRUENAS_CINDER_BRANCH" != HEAD ]] ||
        die "the checkout is on a detached HEAD; check out a branch and re-run"
    if [[ -n "$(as_stack git -C "$PLUGIN_SRC" status --porcelain)" ]]; then
        warn "the checkout has uncommitted changes; DevStack uses the last commit on '$TRUENAS_CINDER_BRANCH'"
    fi

    # DevStack never updates an existing plugin clone (unless RECLONE=True),
    # so drop it to make the next stack.sh pick up this checkout.
    if [[ -d "$PLUGIN_CLONE" ]]; then
        rm -rf -- "$PLUGIN_CLONE"
        info "removed DevStack's previous clone of the plugin at $PLUGIN_CLONE"
    fi
}

# The placeholders local.conf.example may use. Only these are filled in;
# any other placeholder is an error, so a typo in the template cannot pass
# silently and no unrelated environment variable can leak into the output.
readonly TEMPLATE_VARS=(
    ADMIN_PASSWORD HOST_IP LIBVIRT_TYPE
    TRUENAS_IP TRUENAS_LOGIN TRUENAS_API_KEY TRUENAS_VERIFY_SSL TRUENAS_POOL
    TRUENAS_DATASET_ROOT TRUENAS_PORTAL_ID TRUENAS_MULTIPATH
    TRUENAS_API_CLIENT_REF TRUENAS_CINDER_REPO TRUENAS_CINDER_BRANCH
)

# Print devstack/local.conf.example with its placeholders filled in. Done in
# Python so no value is re-interpreted by the shell or by sed. Pass "mask"
# to hide the secrets.
render_local_conf() {
    TRUENAS_MASK_KEY=${1:-} \
        ADMIN_PASSWORD="$ADMIN_PASSWORD" \
        HOST_IP="$HOST_IP" \
        LIBVIRT_TYPE="$LIBVIRT_TYPE" \
        TRUENAS_IP="$TRUENAS_IP" \
        TRUENAS_LOGIN="$TRUENAS_LOGIN" \
        TRUENAS_API_KEY="$TRUENAS_API_KEY" \
        TRUENAS_VERIFY_SSL="$TRUENAS_VERIFY_SSL" \
        TRUENAS_POOL="$TRUENAS_POOL" \
        TRUENAS_DATASET_ROOT="$TRUENAS_DATASET_ROOT" \
        TRUENAS_PORTAL_ID="$TRUENAS_PORTAL_ID" \
        TRUENAS_MULTIPATH="$TRUENAS_MULTIPATH" \
        TRUENAS_API_CLIENT_REF="$TRUENAS_API_CLIENT_REF" \
        TRUENAS_CINDER_REPO="$PLUGIN_SRC" \
        TRUENAS_CINDER_BRANCH="$TRUENAS_CINDER_BRANCH" \
        python3 - "$TEMPLATE" "${TEMPLATE_VARS[@]}" <<'PY'
import os
import re
import sys

template_path, allowed = sys.argv[1], set(sys.argv[2:])
masked = os.environ.get('TRUENAS_MASK_KEY') == 'mask'


def fill(match: re.Match[str]) -> str:
    name = match.group(1)
    if name not in allowed:
        sys.exit(f'local.conf.example uses an unknown placeholder: @{name}@')
    value = os.environ[name]
    if masked and name in ('TRUENAS_API_KEY', 'ADMIN_PASSWORD'):
        value = value[:3] + '...'
    return value


with open(template_path, encoding='utf-8') as template:
    sys.stdout.write(re.sub(r'@([A-Z_]+)@', fill, template.read()))
PY
}

write_local_conf() {
    local target=$DEVSTACK_DIR/local.conf backup
    if [[ -e "$target" ]]; then
        backup=$target.$(date +%Y%m%d-%H%M%S).bak
        cp -p "$target" "$backup"
        log "Saved the existing local.conf as $backup"
    fi
    log "Writing $target"
    # It holds the TrueNAS API key: create it private from the start.
    (
        umask 077
        render_local_conf >"$target"
    )
    chown "$STACK_USER:$STACK_USER" "$target"
}

run_stack() {
    log "Running stack.sh as $STACK_USER; this usually takes 20 to 40 minutes"
    as_stack bash -c "cd '$DEVSTACK_DIR' && ./stack.sh"
}

print_next_steps() {
    printf '\nThe VM is ready.\n\n'
    if [[ "$RUN_STACK" != True ]]; then
        cat <<EOF
Start DevStack (20 to 40 minutes):

    sudo -u $STACK_USER -i
    cd devstack && ./stack.sh

EOF
    fi
    cat <<EOF
Check that the TrueNAS backend is up:

    source $DEVSTACK_DIR/openrc admin admin
    openstack volume service list    # cinder-volume@truenas should be "up"

devstack/README.md has the full checklist, including running Tempest.
EOF
}

dry_run_report() {
    TRUENAS_CINDER_BRANCH=$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)
    log "Dry run: nothing was changed. A real run would:"
    info "install curl, git, python3-venv, open-iscsi and multipath-tools"
    info "check the TrueNAS credentials with tools/smoke_test.py"
    info "enable iscsid and multipathd, writing /etc/multipath.conf if absent"
    info "create the '$STACK_USER' user and clone DevStack ($DEVSTACK_BRANCH) into $DEVSTACK_DIR"
    info "copy this repository to $PLUGIN_SRC (branch $TRUENAS_CINDER_BRANCH)"
    info "write $DEVSTACK_DIR/local.conf as below (secrets masked here)"
    printf '\n'
    render_local_conf mask
}

main() {
    parse_args "$@"
    # Validate the settings before asking for sudo.
    resolve_settings
    if [[ "$DRY_RUN" != True ]]; then
        become_root "$@"
    fi
    preflight_host
    if [[ "$DRY_RUN" == True ]]; then
        preflight_network
        dry_run_report
        return
    fi

    preflight_network
    install_packages
    preflight_credentials
    configure_initiator
    create_stack_user
    clone_devstack
    copy_plugin_repo
    write_local_conf
    if [[ "$RUN_STACK" == True ]]; then
        run_stack
    fi
    print_next_steps
}

main "$@"

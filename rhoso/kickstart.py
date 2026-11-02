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
"""Build the OEMDRV kickstart ISO for the RHOSO compute VM.

The RHEL installer reads ks.cfg from any volume labelled OEMDRV, so attaching
this ISO as a second DVD makes the install unattended without boot-menu
edits. The node's name, address, gateway, DNS servers, and RHEL release come
from an environment's settings file (see settings.sh and env/example.env).
It registers with Red Hat during the install (the boot ISO has no packages),
so it embeds RHSM_ORG_ID and RHSM_ACTIVATION_KEY from the environment: the
output is written mode 0600, and should be deleted once the install is done.

Usage:
    uv run --with pycdlib python rhoso/kickstart.py ENV OUT.iso

ENV is the name of rhoso/env/ENV.env, or a path to a settings file.
"""

from __future__ import annotations

import io
import ipaddress
import os
import pathlib
import re
import sys

import pycdlib

PUBLIC_KEY = pathlib.Path.home() / '.ssh' / 'rhoso-edpm.pub'
SETTINGS = (
    'COMPUTE_NAME',
    'COMPUTE_CTLPLANE_IP',
    'CTLPLANE_PREFIX',
    'CTLPLANE_GATEWAY',
    'DNS_SERVER_1',
    'DNS_SERVER_2',
    'RHEL_RELEASE',
)
_SETTING = re.compile(r'^([A-Z_][A-Z0-9_]*)=(.*)$')

TEMPLATE = """\
# Unattended install of the RHOSO 18 compute (EDPM) node.
text
eula --agreed
lang en_US.UTF-8
keyboard us
timezone UTC --utc
network --device=link --bootproto=static --ip={ip} --netmask={netmask} \
--gateway={gateway} --nameserver={nameservers} --hostname={hostname} \
--activate --onboot=yes
rhsm --organization={org} --activation-key={key}
rootpw --lock
# RHOSO configures compute nodes over SSH as cloud-admin, with passwordless
# sudo.
user --name=cloud-admin --groups=wheel
sshkey --username=cloud-admin "{pubkey}"
zerombr
clearpart --all --initlabel --disklabel=gpt
autopart --type=lvm --nohome
bootloader --timeout=1
selinux --enforcing
# RHOSO manages the node's firewall itself, with nftables.
firewall --disabled
services --enabled=sshd
reboot --eject

%packages
@^minimal-environment
openssh-server
%end

%post --log=/root/ks-post.log
echo 'cloud-admin ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/cloud-admin
chmod 0440 /etc/sudoers.d/cloud-admin
# Stay on {release}: RHOSO 18 does not support later RHEL 9 minor releases.
subscription-manager release --set={release}
%end
"""


def load_settings(env: str) -> dict[str, str]:
    """An environment's settings, read as settings.sh reads them."""
    path = pathlib.Path(env)
    if not path.is_file():
        path = pathlib.Path(__file__).resolve().parent / 'env' / f'{env}.env'
    if not path.is_file():
        sys.exit(f'error: no settings file {env} or {path}')
    settings: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = _SETTING.match(line)
        if not match:
            sys.exit(f'error: {path}: not NAME=value: {line}')
        name, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        settings[name] = value
    missing = [name for name in SETTINGS if not settings.get(name)]
    if missing:
        sys.exit(f'error: not set in {path}: {", ".join(missing)}')
    return settings


def main() -> int:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    settings = load_settings(sys.argv[1])
    org = os.environ.get('RHSM_ORG_ID', '')
    key = os.environ.get('RHSM_ACTIVATION_KEY', '')
    if not org or not key:
        sys.exit('error: RHSM_ORG_ID and RHSM_ACTIVATION_KEY must be set')
    netmask = ipaddress.IPv4Network(
        f'0.0.0.0/{settings["CTLPLANE_PREFIX"]}'
    ).netmask
    kickstart = TEMPLATE.format(
        ip=settings['COMPUTE_CTLPLANE_IP'],
        netmask=netmask,
        gateway=settings['CTLPLANE_GATEWAY'],
        nameservers=f'{settings["DNS_SERVER_1"]},{settings["DNS_SERVER_2"]}',
        hostname=f'{settings["COMPUTE_NAME"]}.localdomain',
        org=org,
        key=key,
        pubkey=PUBLIC_KEY.read_text().strip(),
        release=settings['RHEL_RELEASE'],
    ).encode()

    iso = pycdlib.PyCdlib()
    iso.new(
        interchange_level=3, joliet=3, rock_ridge='1.09', vol_ident='OEMDRV'
    )
    iso.add_fp(
        io.BytesIO(kickstart),
        len(kickstart),
        '/KS.CFG;1',
        rr_name='ks.cfg',
        joliet_path='/ks.cfg',
    )
    out = pathlib.Path(sys.argv[2])
    old_umask = os.umask(0o077)
    try:
        iso.write(str(out))
    finally:
        os.umask(old_umask)
        iso.close()
    print(f'wrote {out} ({out.stat().st_size} bytes, mode 0600)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

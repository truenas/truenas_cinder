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
edits. It registers with Red Hat during the install (the boot ISO has no
packages), so it embeds RHSM_ORG_ID and RHSM_ACTIVATION_KEY from the
environment: the output is written mode 0600, and should be deleted once the
install is done.

Usage:
    uv run --with pycdlib python rhoso/kickstart.py OUT.iso
"""

from __future__ import annotations

import io
import os
import pathlib
import sys

import pycdlib

HOSTNAME = 'edpm-compute-0.localdomain'
CTLPLANE_IP = '192.168.122.100'
GATEWAY = '192.168.122.1'  # the Windows host, NAT to the LAN and internet
NAMESERVERS = '1.1.1.1,8.8.8.8'
RELEASE = '9.6'  # RHOSO 18 supports RHEL 9.4 and 9.6 compute nodes
PUBLIC_KEY = pathlib.Path.home() / '.ssh' / 'rhoso-edpm.pub'

TEMPLATE = """\
# Unattended install of the RHOSO 18 compute (EDPM) node.
text
eula --agreed
lang en_US.UTF-8
keyboard us
timezone UTC --utc
network --device=link --bootproto=static --ip={ip} --netmask=255.255.255.0 \
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


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    org = os.environ.get('RHSM_ORG_ID', '')
    key = os.environ.get('RHSM_ACTIVATION_KEY', '')
    if not org or not key:
        sys.exit('error: RHSM_ORG_ID and RHSM_ACTIVATION_KEY must be set')
    kickstart = TEMPLATE.format(
        ip=CTLPLANE_IP,
        gateway=GATEWAY,
        nameservers=NAMESERVERS,
        hostname=HOSTNAME,
        org=org,
        key=key,
        pubkey=PUBLIC_KEY.read_text().strip(),
        release=RELEASE,
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
    out = pathlib.Path(sys.argv[1])
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

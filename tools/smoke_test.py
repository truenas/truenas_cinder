#!/usr/bin/env python3
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
"""Drive ``TrueNASClient`` against a real TrueNAS system.

This exercises the driver's client adapter directly -- no Cinder, no
DevStack -- so connection, authentication, response shapes, and the ZFS
clone lifecycle can be validated with a fast feedback loop before spending
lab time on a full deployment.

Two modes:

* **probe** (default) -- read-only. Version preflight, authentication, pool
  and portal lookups, capacity parsing. Creates and changes nothing.
* **lifecycle** (``--lifecycle``) -- CREATES AND DESTROYS objects on the
  TrueNAS: a zvol, an iSCSI target/extent, a snapshot, and a clone, all
  named with a unique ``smoke-<uuid>`` tag under a dedicated dataset root.
  Everything is torn down in a ``finally`` block. This is the mode that
  validates the promote/defer clone design on real ZFS.

Configuration comes from the environment (flags override):

    TRUENAS_URL or TRUENAS_IP     TrueNAS endpoint
    TRUENAS_API_KEY               API key  (required)
    TRUENAS_USER                  key owner (default: truenas_admin)
    TRUENAS_AUTH_MECHANISM        PLAIN (default) or SCRAM
    TRUENAS_POOL                  ZFS pool (default: tank)
    TRUENAS_ISCSI_PORTAL          portal id, or host:port to resolve by IP
    TRUENAS_INSECURE_SKIP_VERIFY  set true to skip TLS verification

Usage:
    uv run python tools/smoke_test.py              # read-only
    uv run python tools/smoke_test.py --lifecycle  # destructive
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from truenas_cinder import common  # noqa: E402
from truenas_cinder.client import (  # noqa: E402
    AUTH_MECHANISMS,
    AUTH_PLAIN,
    REQUIRED_API_VERSION,
    TrueNASClient,
)

TRUTHY = {'1', 'true', 'yes', 'y', 'on'}


class Reporter:
    """Collects PASS/FAIL/INFO lines and exits non-zero on any failure."""

    def __init__(self) -> None:
        self.failures: int = 0

    def ok(self, label: str, detail: str = '') -> None:
        print(f'  [ OK ] {label}' + (f' -- {detail}' if detail else ''))

    def fail(self, label: str, detail: str = '') -> None:
        self.failures += 1
        print(f'  [FAIL] {label}' + (f' -- {detail}' if detail else ''))

    def info(self, label: str, detail: str = '') -> None:
        print(f'  [info] {label}' + (f' -- {detail}' if detail else ''))

    def section(self, title: str) -> None:
        print(f'\n== {title} ==')


def build_url(args: argparse.Namespace) -> str:
    if args.url:
        return str(args.url)
    host = args.host or os.environ.get('TRUENAS_IP', '')
    if not host:
        sys.exit('error: set TRUENAS_URL or TRUENAS_IP (or pass --url/--host)')
    return f'wss://{host}/api/current'


def portal_id_from_env(client: TrueNASClient, raw: str, rep: Reporter) -> int:
    """Accept either a numeric portal id or a ``host:port`` to resolve."""
    candidate = raw.split(':', 1)[0] if ':' in raw else raw
    if candidate.isdigit() and ':' not in raw:
        return int(candidate)
    # An address was given: find the portal listening on it (or a wildcard).
    for portal_id in range(1, 17):
        portal = client.get_portal(portal_id)
        if portal is None:
            continue
        ips = client.portal_listen_ips(portal_id)
        if candidate in ips or '0.0.0.0' in ips or '::' in ips:
            rep.info('portal resolved by address', f'{raw} -> id={portal_id}')
            return portal_id
    rep.info('portal not resolved by address; defaulting to id=1', raw)
    return 1


def probe(
    client: TrueNASClient, args: argparse.Namespace, rep: Reporter
) -> int:
    """Read-only checks. Returns the resolved portal id."""
    rep.section('Pool')
    pool = client.get_pool(args.pool)
    if pool is None:
        rep.fail('pool.query', f'pool {args.pool!r} not found')
    else:
        rep.ok(
            'pool.query',
            f'{args.pool} status={pool.get("status")} '
            f'healthy={pool.get("healthy")}',
        )
        size = pool.get('size')
        rep.info(
            'size/allocated types',
            f'size={type(size).__name__} '
            f'allocated={type(pool.get("allocated")).__name__}',
        )

    free = client.available_bytes(args.pool)
    if free > 0:
        rep.ok(
            'zfs.resource.query available',
            f'{common.bytes_to_gib(free):.1f} GiB free',
        )
    else:
        rep.fail('zfs.resource.query available', 'parsed 0 bytes free')

    rep.section('iSCSI portal')
    portal_id = portal_id_from_env(client, args.portal, rep)
    portal = client.get_portal(portal_id)
    if portal is None:
        rep.fail('iscsi.portal.query', f'portal id {portal_id} not found')
        return portal_id
    ips = client.portal_listen_ips(portal_id)
    rep.ok('iscsi.portal.query', f'id={portal_id} listen={ips}')
    if any(ip in ('0.0.0.0', '::') for ip in ips):
        rep.info(
            'wildcard bind detected',
            'driver substitutes truenas_ip for the portal address',
        )
    return portal_id


def lifecycle(
    client: TrueNASClient,
    args: argparse.Namespace,
    portal_id: int,
    rep: Reporter,
) -> None:
    """Destructive create/clone/promote/delete cycle with full teardown."""
    tag = f'smoke-{uuid.uuid4().hex[:8]}'
    root = args.dataset_root
    src = common.dataset_name(args.pool, root, tag)
    clone = common.dataset_name(args.pool, root, f'{tag}-clone')
    snap_name = f'snap-{tag}'
    snap_id = common.snapshot_id(src, snap_name)
    size = common.gib_to_bytes(1)

    created: dict[str, int] = {}
    rep.section(f'Lifecycle (tag {tag})')
    try:
        # -- zvol ---------------------------------------------------------
        client.create_zvol(
            src,
            size,
            volblocksize='16K',
            compression='LZ4',
            sparse=True,
        )
        rep.ok('pool.dataset.create', src)
        dataset = client.get_dataset(src)
        if dataset is None:
            rep.fail(
                'pool.dataset.get_instance', 'zvol not found after create'
            )
            return
        rep.ok('pool.dataset.get_instance', 'volsize/refreservation readable')

        # -- iSCSI chain --------------------------------------------------
        zvol = common.zvol_device_path(src)
        extent = client.create_extent(common.extent_name(tag), zvol)
        created['extent'] = int(extent['id'])
        rep.ok('iscsi.extent.create', f'id={extent["id"]} disk={zvol}')

        initiator = client.create_initiator([], common.target_name(tag))
        created['initiator'] = int(initiator['id'])
        rep.ok('iscsi.initiator.create', f'id={initiator["id"]}')

        target = client.create_target(
            common.target_name(tag),
            f'Cinder smoke test {tag}',
            [
                {
                    'portal': portal_id,
                    'initiator': int(initiator['id']),
                    'authmethod': 'NONE',
                }
            ],
        )
        created['target'] = int(target['id'])
        rep.ok('iscsi.target.create', f'id={target["id"]}')

        te = client.create_targetextent(int(target['id']), int(extent['id']))
        created['targetextent'] = int(te['id'])
        rep.ok(
            'iscsi.targetextent.create', f'id={te["id"]} lun={te.get("lunid")}'
        )

        # -- snapshot + clone --------------------------------------------
        client.create_snapshot(src, snap_name)
        rep.ok('pool.snapshot.create', snap_id)

        client.clone_snapshot(snap_id, clone)
        rep.ok('pool.snapshot.clone', clone)

        detected = client.clones_of_snapshot(snap_id)
        if clone in detected:
            rep.ok('clone detection via origin', f'{detected}')
        else:
            rep.fail(
                'clone detection via origin', f'expected {clone} in {detected}'
            )

        # -- the design's two critical ZFS behaviours ---------------------
        # 1. defer:false on a snapshot with clones must be refused.
        try:
            client.delete_snapshot(snap_id, defer=False)
            rep.fail(
                'snapshot delete defer=false with clone',
                'succeeded, but ZFS was expected to refuse it',
            )
        except Exception as exc:
            rep.ok(
                'snapshot delete defer=false with clone refused',
                type(exc).__name__,
            )

        # 2. defer:true is accepted and the snapshot survives as the clone's
        #    origin (ZFS marks defer_destroy and reaps it with the last clone,
        #    so it stays visible in the meantime).
        client.delete_snapshot(snap_id, defer=True)
        if client.get_snapshot(snap_id) is not None:
            rep.ok(
                'snapshot delete defer=true accepted',
                'still listed, pending last-clone release',
            )
        else:
            rep.info('snapshot delete defer=true', 'removed immediately')

        # 3. destroying the source while a clone depends on it must fail.
        #    Note: TrueNAS tears down the zvol's iSCSI attachments (extent,
        #    target, targetextent) as part of this call even when the ZFS
        #    destroy then fails, so those ids may already be gone below.
        try:
            client.delete_dataset(src, recursive=True, force=True)
            rep.fail(
                'source delete with dependent clone',
                'succeeded, but ZFS was expected to refuse it',
            )
        except Exception as exc:
            rep.ok(
                'source delete with dependent clone refused',
                type(exc).__name__,
            )

        # 4. promote re-parents the clone so the source can be destroyed.
        deps = client.dependent_clones(src)
        rep.info('dependent_clones(src)', f'{deps}')
        client.promote_dataset(clone)
        rep.ok('pool.dataset.promote', clone)
        after = client.dependent_clones(src)
        if not after:
            rep.ok('source detached after promote', 'no dependent clones')
        else:
            rep.fail('source detached after promote', f'still {after}')

        # -- teardown of the iSCSI chain before deleting the zvol ---------
        client.delete_targetextent(created.pop('targetextent'))
        client.delete_target(created.pop('target'))
        client.delete_extent(created.pop('extent'))
        client.delete_initiator(created.pop('initiator'))
        rep.ok('iSCSI chain teardown', 'targetextent/target/extent/initiator')

        client.delete_dataset(src, recursive=True, force=True)
        if client.get_dataset(src) is None:
            rep.ok('source delete after promote', src)
        else:
            rep.fail('source delete after promote', 'still present')

        # Does the promoted clone still carry the deferred snapshot?
        moved = common.snapshot_id(clone, snap_name)
        still = client.get_snapshot(moved)
        rep.info(
            'deferred snapshot after promote',
            f'{moved} -> {"present" if still else "gone"}',
        )
    except Exception:
        rep.fail('lifecycle raised', traceback.format_exc(limit=3))
    finally:
        rep.section('Cleanup')
        for kind, obj_id in list(created.items()):
            try:
                {
                    'targetextent': client.delete_targetextent,
                    'target': client.delete_target,
                    'extent': client.delete_extent,
                    'initiator': client.delete_initiator,
                }[kind](obj_id)
                rep.info(f'removed {kind}', str(obj_id))
            except Exception as exc:
                rep.fail(f'cleanup {kind} {obj_id}', str(exc)[:120])
        # A promote inverts the origin relationship, so whichever dataset now
        # holds the shared snapshot must go last. Retry both orders rather
        # than assume which way round the pair ended up.
        pending = [src, clone]
        for _ in range(len(pending)):
            for path in list(pending):
                try:
                    client.delete_dataset(path, recursive=True, force=True)
                except Exception:
                    continue
                if client.get_dataset(path) is None:
                    pending.remove(path)
                    rep.info('removed dataset', path)
        for path in pending:
            rep.fail('dataset still present', path)
        if not pending:
            # Remove the dataset root we created, but non-recursively so a
            # concurrent run's volumes are never destroyed.
            root_path = f'{args.pool}/{root}'
            try:
                client.delete_dataset(root_path, recursive=False, force=False)
                rep.info('removed dataset root', root_path)
            except Exception:
                rep.info('left dataset root in place', root_path)


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=env.get('TRUENAS_URL'))
    parser.add_argument('--host', default=None)
    parser.add_argument(
        '--user', default=env.get('TRUENAS_USER', 'truenas_admin')
    )
    parser.add_argument('--pool', default=env.get('TRUENAS_POOL', 'tank'))
    parser.add_argument(
        '--portal', default=env.get('TRUENAS_ISCSI_PORTAL', '1')
    )
    parser.add_argument(
        '--dataset-root',
        default=env.get('TRUENAS_DATASET_ROOT', 'cinder-smoketest'),
    )
    parser.add_argument(
        '--auth-mechanism',
        choices=AUTH_MECHANISMS,
        default=env.get('TRUENAS_AUTH_MECHANISM', AUTH_PLAIN),
    )
    parser.add_argument(
        '--verify-ssl',
        action='store_true',
        help='Verify TLS (default: follows TRUENAS_INSECURE_SKIP_VERIFY).',
    )
    parser.add_argument(
        '--lifecycle',
        action='store_true',
        help='Run the DESTRUCTIVE create/clone/promote/delete '
        'cycle (objects are cleaned up afterwards).',
    )
    args = parser.parse_args(argv)

    api_key = env.get('TRUENAS_API_KEY', '')
    if not api_key:
        return int(
            bool(sys.stderr.write('error: TRUENAS_API_KEY is not set\n'))
        )

    insecure = env.get('TRUENAS_INSECURE_SKIP_VERIFY', '').lower() in TRUTHY
    verify_ssl = args.verify_ssl or not insecure
    url = build_url(args)

    rep = Reporter()
    print(
        f'TrueNAS smoke test -- {url} as {args.user!r} '
        f'({args.auth_mechanism}, verify_ssl={verify_ssl})'
    )

    rep.section('Preflight')
    advertised = TrueNASClient.api_versions(url, verify_ssl=verify_ssl)
    if not advertised:
        rep.info('GET /api/versions', 'unreachable (advisory only)')
    elif REQUIRED_API_VERSION in advertised:
        rep.ok('GET /api/versions', f'advertises {REQUIRED_API_VERSION}')
    else:
        rep.fail(
            'GET /api/versions', f'{REQUIRED_API_VERSION} not in {advertised}'
        )

    client = TrueNASClient()
    if not client.dependency_available():
        rep.fail('truenas_api_client', 'not installed')
        return 1
    try:
        client.connect(
            url,
            args.user,
            api_key,
            verify_ssl=verify_ssl,
            auth_mechanism=args.auth_mechanism,
        )
    except Exception as exc:
        rep.fail('authenticate', str(exc)[:300])
        print('\nRESULT: FAILED (could not authenticate)')
        return 1
    rep.ok('authenticate', f'{args.auth_mechanism} as {args.user!r}')

    try:
        client.ping()
        rep.ok('core.ping', 'pong')
        portal_id = probe(client, args, rep)
        if args.lifecycle:
            lifecycle(client, args, portal_id, rep)
        else:
            rep.info(
                'lifecycle skipped',
                'pass --lifecycle to exercise create/clone/promote',
            )
    finally:
        client.close()

    print(
        f'\nRESULT: {"FAILED" if rep.failures else "PASSED"} '
        f'({rep.failures} failure(s))'
    )
    return 1 if rep.failures else 0


if __name__ == '__main__':
    sys.exit(main())

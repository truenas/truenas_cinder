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
"""One-time bootstrap: copy the TrueNAS driver + unit tests into an
``openstack/cinder`` checkout at their in-tree paths, rewriting the dev-repo
package imports to the in-tree package path.

Dev repo layout                         In-tree (cinder) layout
------------------------------------    ------------------------------------
src/truenas_cinder/*.py             ->  cinder/volume/drivers/truenas/*.py
tests/unit/{fakes,test_*}.py        ->  cinder/tests/unit/volume/drivers/
                                            truenas/*.py

Import rewrites applied to every copied file:
    truenas_cinder   ->  cinder.volume.drivers.truenas
    tests.unit       ->  cinder.tests.unit.volume.drivers.truenas

This is a **bootstrap** for the initial Gerrit push, NOT an ongoing two-way
sync. Once review starts, the fork/Gerrit is authoritative; re-running this
would clobber reviewer edits, so existing destination files are skipped
unless ``--force`` is given.

Usage:
    python tools/sync_to_cinder.py /path/to/cinder [--dry-run] [--force]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_PKG = REPO_ROOT / 'src' / 'truenas_cinder'
TEST_PKG = REPO_ROOT / 'tests' / 'unit'

# Destinations relative to the cinder checkout root.
DRIVER_DEST = Path('cinder/volume/drivers/truenas')
TEST_DEST = Path('cinder/tests/unit/volume/drivers/truenas')

# The driver package: order mirrors the dependency graph for readability.
DRIVER_MODULES = (
    '__init__.py',
    'common.py',
    'exception.py',
    'opts.py',
    'client.py',
    'driver.py',
)
# Test modules (an __init__.py is generated for the destination package).
TEST_MODULES = (
    'fakes.py',
    'test_common.py',
    'test_driver.py',
)

IN_TREE_PKG = 'cinder.volume.drivers.truenas'
IN_TREE_TEST_PKG = 'cinder.tests.unit.volume.drivers.truenas'

_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r'\btruenas_cinder\b'), IN_TREE_PKG),
    (re.compile(r'\btests\.unit\b'), IN_TREE_TEST_PKG),
)

_INIT_HEADER = """\
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
"""

_CHECKLIST = """
Next steps in the cinder fork (this tool does not do these):
  1. Add a reno note:  releasenotes/notes/truenas-iscsi-driver-*.yaml
  2. Declare the backend in doc/source/reference/support-matrix.ini and add
     doc/source/configuration/block-storage/drivers/truenas-driver.rst
  3. Regenerate options:            tox -e genopts
  4. Verify interface compliance:   tox -e compliance
  5. Style (final authority, ruff only approximates it): tox -e pep8
     -- fix any hacking import-order (H3xx) nits the rewrite left behind.
  6. Run the ported unit tests:     tox -e py3 -- volume.drivers.truenas
"""


def rewrite_imports(text: str) -> str:
    """Rewrite dev-repo package references to their in-tree paths."""
    for pattern, replacement in _REWRITES:
        text = pattern.sub(replacement, text)
    return text


class Syncer:
    def __init__(
        self,
        cinder_root: Path,
        *,
        dry_run: bool,
        force: bool,
    ) -> None:
        self.cinder_root = cinder_root
        self.dry_run = dry_run
        self.force = force
        self.written: list[Path] = []
        self.skipped: list[Path] = []

    def _emit(self, dest: Path, content: str) -> None:
        rel = dest.relative_to(self.cinder_root)
        if dest.exists() and not self.force:
            self.skipped.append(rel)
            return
        self.written.append(rel)
        if self.dry_run:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding='utf-8')

    def _copy_module(self, source: Path, dest: Path) -> None:
        content = rewrite_imports(source.read_text(encoding='utf-8'))
        self._emit(dest, content)

    def run(self) -> None:
        driver_dir = self.cinder_root / DRIVER_DEST
        test_dir = self.cinder_root / TEST_DEST

        for name in DRIVER_MODULES:
            self._copy_module(SRC_PKG / name, driver_dir / name)

        # The destination test package needs its own __init__.py.
        self._emit(test_dir / '__init__.py', _INIT_HEADER)
        for name in TEST_MODULES:
            self._copy_module(TEST_PKG / name, test_dir / name)


def _validate_cinder_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    if not (root / 'cinder' / 'volume' / 'drivers').is_dir():
        raise SystemExit(
            f'error: {root} does not look like an openstack/cinder checkout '
            '(missing cinder/volume/drivers).'
        )
    return root


def _report(syncer: Syncer) -> None:
    verb = 'Would write' if syncer.dry_run else 'Wrote'
    summary = 'to write' if syncer.dry_run else 'written'
    for rel in syncer.written:
        print(f'  {verb}: {rel}')
    for rel in syncer.skipped:
        print(f'  Skipped (exists; use --force): {rel}')
    print(
        f'\n{len(syncer.written)} file(s) {summary}, '
        f'{len(syncer.skipped)} skipped.'
    )
    if syncer.written and not syncer.skipped:
        print(_CHECKLIST)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        'cinder_root',
        type=Path,
        help='Path to the openstack/cinder checkout (fork) root.',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be copied without writing anything.',
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Overwrite destination files that already exist. Bootstrap '
        'only -- this clobbers any edits made in the fork.',
    )
    args = parser.parse_args(argv)

    cinder_root = _validate_cinder_root(args.cinder_root)
    syncer = Syncer(cinder_root, dry_run=args.dry_run, force=args.force)
    syncer.run()
    _report(syncer)
    return 0


if __name__ == '__main__':
    sys.exit(main())

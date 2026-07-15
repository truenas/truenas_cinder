; Additions for doc/source/reference/support-matrix.ini in the cinder fork.
;
; This is a MERGE GUIDE, not a standalone file. support-matrix.ini is a shared
; cinder file: add the [driver.truenas] section once, then add the
; `driver.truenas=...` line shown below into each EXISTING [operation.*]
; section (do not duplicate the operation headers). Values are `complete` or
; `missing`. Every `complete` must be backed by passing third-party CI.

; -- New driver section (add alongside the other [driver.*] sections) --------
[driver.truenas]
title=TrueNAS Storage Driver (iSCSI)

; -- Per-operation support (add one line into each existing section) ---------

[operation.supported]
; Requires green third-party CI within a 2-week window to stay `complete`.
driver.truenas=complete

[operation.online_extend_support]
driver.truenas=complete

[operation.qos]
driver.truenas=missing

[operation.volume_replication]
driver.truenas=missing

[operation.consistency_groups]
driver.truenas=missing

[operation.thin_provisioning]
driver.truenas=complete

[operation.volume_migration_storage_assisted]
; Host-assisted migration only (base-class fallback); no storage-assisted path.
driver.truenas=missing

[operation.multi-attach]
driver.truenas=complete

[operation.revert_to_snapshot_assisted]
; ZFS pool.snapshot.rollback (latest snapshot only).
driver.truenas=complete

[operation.active_active_ha]
driver.truenas=missing

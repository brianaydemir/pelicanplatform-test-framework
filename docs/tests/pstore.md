# pstore

Part of [What the tests check](README.md).

A pstore origin
([`-p origin-pstore`](../../framework/presets/origin-pstore.sh)) keeps its
objects encrypted, in block files and a catalog that only the running origin
can read (Pelican's
[pstore design](https://github.com/PelicanPlatform/pelican/blob/main/docs/pstore-design.md),
whose sections this page cites). The
[`pstore`](../../framework/suites/pstore.py) suite checks its admin storage
API and CLI, its scheduled passes, its size bound, and its catalog's
backups; [`blocks`](block-boundaries.md) and
[`posc`](posc-and-metadata.md#posc) check how it serves and stores objects.
[Names](names.md), [listings](listings.md), and
[client commands](client-commands.md) also use the storage API.

The suite writes in the first of `/protected-a` and `/protected-b` that
takes writes, and skips where neither does. At other
[native](../glossary.md#native-and-xrootd) origins, only `storage-api`'s
`absent` rows run; XRootD origins are skipped. A
[case](../glossary.md#case)'s row is named `<scenario> <case>`, or
`<scenario> <origin> <case>` where each origin is asked. `introspect-cli`,
`scheduled-passes`, `corruption`, and `backups` run in the
[WLCG pass](../glossary.md#pass) only
([token profiles](README.md#token-profiles)).

- `storage-api`: Each pstore origin must serve an admin-only storage API on
  its web port: `GET /api/v1.0/origin/storage/pstore/ls/<path>`,
  `stat/<path>`, `digest/<path>`, and `usage`. It is asked of a collection
  of seven objects and a subcollection, `sub`:

  - `authz`: Each route must answer 401 with no token or the `server`
    [credential](../glossary.md#credential)'s token, 403 for an ordinary
    user, and 200 for the admin.
  - `ls`: The collection's eight entries in name order, with path, size, and
    kind; with `limit=3` and `after`, pages of 3, 3, and 2, `more` on all
    but the last. Of an object, 400; of nothing, 404.
  - `stat`: Of an object, its name, path, size, and ETag, as a HEAD gives
    them; of `sub`, a collection; of nothing, 404.
  - `digest`: Of an object, its md5, sha, and crc32c
    ([RFC 3230](https://www.rfc-editor.org/rfc/rfc3230)), as a HEAD with
    `Want-Digest` gives them; of a collection, 400.
  - `usage`: `backend` `pstore`, at least the collection's bytes, and a
    capacity of 0. `pelican_pstore_used_bytes` and
    `pelican_pstore_limit_bytes` must agree within 45 seconds.
  - `absent`: Every route must get 404 at a native origin without a pstore.

- `introspect-cli`: `pelican-server origin introspect --json` at the first
  pstore origin. Skips without `pelican-server`
  ([client binary](../../README.md#client-binary)).

  - `token`: With the admin's token file, `usage`, `ls`, `stat`, and
    `digest` must print what the API answers.
  - `missing`: With a missing token file, it must fail, and say so.
  - `minted`: With no token file, given the origin's issuer key and web URL,
    it must mint an admin token that the origin takes.

- `scheduled-passes`: The [shape](../glossary.md#shape) runs the index check
  (`Origin.PStoreIndexCheckInterval`) and the data scan
  (`Origin.PStoreDataScanInterval`) every minute. At each pstore origin,
  with one object overwritten and another deleted, within 150 seconds
  `pelican_pstore_reclaimed_objects_total` must rise by 2,
  `pelican_pstore_reclaimed_bytes_total` by their sizes, and each pass must
  run again. No pass may fail, the index check must find nothing
  (`pelican_pstore_fsck_findings`), and the data scan must count no
  mismatches. The index check's `pending_instances` are noted, not failed
  ([section 11.2](https://github.com/PelicanPlatform/pelican/blob/main/docs/pstore-design.md#112-fsck)).

- `corruption`: At each pstore origin, with a byte of an object's block
  inverted on disk, the next data scan must count it within 150 seconds
  (`pelican_pstore_data_scan_mismatches_total`;
  [section 11.3](https://github.com/PelicanPlatform/pelican/blob/main/docs/pstore-design.md#113-scheduled-integrity-checks)).

`capacity`, `backups`, and `restore` restart the first origin, as the last
of its [configuration layers](../glossary.md#configuration-layer)
([service control](../../README.md#service-control)): its storage bounded at
what it holds plus 8 MiB (`Origin.PStoreStorageDirs`), and its catalog
backed up every 20 seconds, keeping 3 (`Origin.PStoreMetadataBackup*`). This
needs a [control helper](../glossary.md#service-control-helper) and a
[topology](../glossary.md#topology) other than
[`tiny`](../../framework/presets/topo-tiny.sh); elsewhere the three skip.

- `capacity`: Past its bound, a pstore write must get 507 until something is
  deleted
  ([section 7](https://github.com/PelicanPlatform/pelican/blob/main/docs/pstore-design.md#7-capacity)),
  each refusal counted once in
  `pelican_pstore_write_failures_total{reason="no_space"}`:

  - `fill`: Objects written until 512 KiB are left must grow usage by their
    bytes and a tag per block, less what the janitor frees.
  - `sized`: A PUT with a `Content-Length` 256 KiB more than is left must
    get 507 before its body is sent.
  - `expect`: The same, with `Expect: 100-continue`, and no `100 Continue`
    first.
  - `chunked`: A chunked PUT of 128 KiB more than is left must get 507, and
    leave nothing.
  - `freed`: After a DELETE, `sized`'s PUT must succeed within 90 seconds.
  - `gauges`: `pelican_pstore_limit_bytes` must be the bound, and both
    gauges must agree with `usage` within 45 seconds.

- `backups`: The origin must back up its catalog at startup, then an
  interval after each backup, to `pstore-metadata-<UTC>.pmb` by way of a
  `.partial` file, keeping the newest
  ([section 11.4](https://github.com/PelicanPlatform/pelican/blob/main/docs/pstore-design.md#114-metadata-backup-and-restore)).

  - `startup`: A backup within 60 seconds of the restart, its pass counted
    in `pelican_pstore_scheduled_passes_total{pass="metadata_backup"}`.
  - `schedule`: After four passes, backups 20 to 30 seconds apart, and no
    pass failed.
  - `files`: Three backups, each starting `PELICAN-PSTORE-BACKUP\0\1`, and
    nothing else; `pelican_pstore_metadata_backup_last_size_bytes` the
    newest one's size.

- `restore`: The offline CLI, `pelican-server origin pstore`, must restore a
  backup (sections
  [11.4](https://github.com/PelicanPlatform/pelican/blob/main/docs/pstore-design.md#114-metadata-backup-and-restore)
  and
  [11.5](https://github.com/PelicanPlatform/pelican/blob/main/docs/pstore-design.md#115-recovery-export)).
  Destructive: the origin is stopped and its catalog (`db/`) set aside.
  Skips without `pelican-server`.

  - `objects`: Five objects are written, and the next backup kept. A sixth,
    written after it, must be served.
  - `cli`: `metadata-restore` must restore that backup, given the origin's
    issuer keys.
  - `export`: `export --verify` must write out every object the backup held,
    byte for byte, and not the sixth.
  - `served`: Started on the restored catalog, the origin must serve the
    five objects and answer 404 for the sixth. If it does not, the original
    catalog is put back.

# pstore

Part of [What the tests check](README.md).

A pstore origin (`-p origin-pstore`) keeps its objects encrypted, in
block files and in a catalog that only the running origin can read
(`pstore/` and `origin_serve/backend_pstore.go` in Pelican). Besides
serving objects, which `blocks` and `posc` check, it has an
administrative storage API and CLI, verification passes that run on a
schedule, a size bound past which it refuses writes, and backups of its
catalog. The `pstore` suite checks them. Its objects go under
`/<namespace>/data/pstore/<run>/`, in the first of `/protected-a` and
`/protected-b` that takes writes, at every pstore origin, and it
removes them afterward. At another native origin, only `storage-api`'s
`absent` row runs, and the suite skips XRootD origins. A scenario with
several cases records a row per case, named `<scenario> <case>`, or
`<scenario> <origin> <case>` where it asks each origin.

- `storage-api`: each pstore origin serves, on its web port, `GET
  /api/v1.0/origin/storage/pstore/ls/<path>`, `stat/<path>`,
  `digest/<path>`, and `usage`, for its admin alone
  (`origin_serve/storage_api.go`). A path is in the store's catalog,
  under the export's `StoragePrefix` (`/protected-a/...`). At each
  pstore origin, the suite puts a collection of seven objects, of 100 to
  18,100 bytes, some held in the catalog and some in block files, and a
  collection `sub`, which holds one.
  - `authz`: each route, with no token, must get 401; with the
    namespace's `server` token, which is not of the origin's making for
    its web API, 401; with the web token of an ordinary user,
    `test-pstore` (`credentials.mint_user()`), 403; and with the
    admin's, 200. The routes take two handlers, one to say who the
    caller is, and one whether they are the admin; registered alone,
    the second would refuse everyone with 401.
  - `ls`: of the collection, whole, its eight entries in name order,
    each with its path, size, and kind, and `more` false. Three at a
    time (`limit=3`), each page continued with `after` set to the last
    name before it: pages of three, three, and two entries, `more` on
    all but the last, together the whole listing, with none twice. `ls`
    of an object must get 400, and of nothing, 404.
  - `stat`: of an object in a block file, and of one in the catalog,
    its name, path, size, and ETag, each as a HEAD at the data port
    says them; of `sub`, that it is a collection, with no ETag; and of
    nothing, 404.
  - `digest`: of an object, the RFC 3230 checksums that the store
    recorded as it wrote it, md5, sha, and crc32c
    (`pstore/checksum.go`), each the object's, and each what a HEAD
    with `Want-Digest` of it alone gets at the data port; of a
    collection, 400.
  - `usage`: `backend` `pstore`, at least the collection's bytes used,
    and a capacity of 0, since the shape bounds no store. Within 45
    seconds, `pelican_pstore_used_bytes` and
    `pelican_pstore_limit_bytes` must say the same: the janitor
    republishes them every 30 seconds.
  - `absent`: at a native origin whose store is not a pstore, every
    route, with no token or with the admin's, must be 404, since the
    origin registers none.
- `introspect-cli`: `pelican-server origin introspect` (the client
  lacks it; see [client binary](../../README.md#client-binary)) asks the
  first pstore origin's storage API, with `--server` and `--json`.
  `token`: with the admin's token in a file (`--token`), `usage`, and
  `ls`, `stat`, and `digest` of `storage-api`'s collection, must print
  what the API answers. `missing`: with a token file that does not
  exist, it must fail, and say that the file was not found. `minted`:
  with no file, it mints an admin token with its host's issuer key, for
  the issuer that the origin's web API takes. Given a copy of the
  origin's key (`PELICAN_ISSUERKEYSDIRECTORY`; a copy, since Pelican
  makes a key in a key directory that has none) and the origin's web
  URL (`PELICAN_SERVER_EXTERNALWEBURL`), `usage` must print what the
  API answers. The scenario skips without a `pelican-server`.
- `scheduled-passes`: a pstore origin checks its catalog
  (`Origin.PStoreIndexCheckInterval`), and reads every object back
  against its checksums (`Origin.PStoreDataScanInterval`), on a
  schedule: here every minute, the first a minute after it starts
  (`config.d/origin/pstore/50-origin.yaml`). Its janitor frees, every
  30 seconds, the versions that an overwrite or a delete replaced. At
  each pstore origin, once each pass has succeeded, within 150 seconds,
  the suite puts two objects of 100,000 bytes, then overwrites one and
  deletes the other. Within 150 seconds,
  `pelican_pstore_reclaimed_objects_total` must rise by 2, and
  `pelican_pstore_reclaimed_bytes_total` by their sizes; and each pass
  must begin again, and succeed. No pass may have failed
  (`pelican_pstore_scheduled_pass_failures_total`); the index check
  must find nothing (`pelican_pstore_fsck_findings`); and the data scan
  must have verified objects, and found none changed
  (`pelican_pstore_data_scan_mismatches_total`). The index check's
  `pending_instances`, writes under way or abandoned less than 10
  minutes ago, which only the janitor reclaims, are reported but not
  failed (`pstore/fsck.go`): a copy that failed in the `exports` suite
  may leave some.

No shape bounds a store, or backs one up. So for `capacity`, `backups`,
and `restore`, the suite restarts the first origin, once, as the last of
its configuration layers ([service control](../../README.md#service-control)).
Its one storage directory, `/data/pstore`, is bounded at what the
storage API says it holds and 8 MiB more (`Origin.PStoreStorageDirs`,
with a `MaxSize`). It backs up its catalog every 20 seconds, keeping 3
(`Origin.PStoreMetadataBackupLocation`, `PStoreMetadataBackupInterval`,
and `PStoreMetadataBackupsToKeep`), to a directory of the run's own in
its state, `framework/var/state/<service>/pstore-backups/<run>/`, which
the suite removes afterward. The suite then restores the origin, and
waits, as `freshness` does, until every director names every origin and
every cache answers. The restart needs a control helper, and skips the
`tiny` topology, whose origin is the whole federation's process;
otherwise the three scenarios skip, and say why. `scheduled-passes` runs
before it, since a restart starts the passes over.

- `capacity`: straight at the origin, in order. A pstore never evicts:
  past its bound, a write gets 507 until something is deleted
  (`pstore/capacity.go`). Each refusal must count once in
  `pelican_pstore_write_failures_total{reason="no_space"}`.
  - `fill`: objects of up to 960 KiB, each written whole, with its
    length, below pstore's 1 MiB spill threshold, so that each takes
    its bytes and a tag per block, until 512 KiB are left. The store's
    usage must have grown by what they take, less what the janitor
    freed meanwhile.
  - `sized`: a PUT's headers, with a `Content-Length` 256 KiB more than
    is left, and no body: the origin must answer 507, and nothing else,
    before any of the body comes. It checks a declared length against
    the store before it reads (`checkBackendCapacity` in
    `origin_serve/handlers.go`).
  - `expect`: the same, with `Expect: 100-continue`: 507, with no `100
    Continue` first. Python's `http.client` passes over a `100
    Continue`, and sends a body whole, so these two send a request of
    their own (`web.statuses_before_body()`).
  - `chunked`: a PUT in chunks, with no length, of 128 KiB more than is
    left, which the origin can refuse only once it has read what is
    left, must get 507, not the WebDAV handler's 405 nor a 500
    (`statusForBackendError` in `origin_serve/error_handling.go`). It
    must leave nothing behind: a HEAD of the object and its `stat` must
    get 404, and the store's usage must not have grown. What the origin
    leaves unread is less than the 256 KiB that Go's server reads and
    discards before it answers, so that it need not hang up on a
    client still sending.
  - `freed`: after a DELETE of a filler, the store must have room for
    `sized`'s object within 90 seconds, once the janitor frees the
    filler; `sized`'s PUT, with its body, must then succeed, read back
    whole, and count no refusal.
  - `gauges`: `pelican_pstore_limit_bytes` must be the bound, and with
    `pelican_pstore_used_bytes`, within 45 seconds, say what the storage
    API's `usage` says.
- `backups`: the origin backs up its catalog at startup, then an
  interval after each backup, to `pstore-metadata-<UTC>.pmb`, through a
  `.partial` file that it renames, and prunes all but the newest it
  keeps (`snapshotOnce` in `pstore/backup.go`).
  - `startup`: within 60 seconds of the restart, a backup must appear,
    written within an interval of when the origin started
    (`process_start_time_seconds`), with its pass counted
    (`pelican_pstore_scheduled_passes_total{pass="metadata_backup"}`).
  - `schedule`: once four backup passes have begun, the backups must be
    20 seconds apart, give or take 10 (the interval runs from the end of
    one to the start of the next), and none of the passes may have
    failed.
  - `files`: the directory must then hold three backups, the startup
    one pruned, each named for when it was written and beginning with
    the format's magic and version, `PELICAN-PSTORE-BACKUP\0\1`
    (`pstore/backup_crypto.go`), and no `.partial` file, nor anything
    else; and `pelican_pstore_metadata_backup_last_size_bytes` must be
    the newest one's size.
- `restore`, with the origin still backing up its catalog so: a backup
  is only as good as what comes back out of it, and nothing but the
  origin can read the store, so the scenario checks a restore through
  the offline CLI, which reads the store on its own
  (`pelican-server origin pstore`; `pstore/backup.go` and `recover.go`).
  - `objects`: five objects of 100 bytes to 1 MiB and 5, held in the
    catalog and in block files, written straight to the origin; the
    first backup taken after them, copied out of its directory; and
    then one more object, written after that backup, which the origin
    must serve.
  - `cli`: with the origin stopped, the store's catalog (`db/`) is set
    aside, leaving its block files (`objects/`) and master key
    (`masterkey.json`), as a recovery is laid out, and
    `metadata-restore` must restore the copied backup into it, opened
    with a copy of the origin's issuer keys, to which the catalog's
    master key and every backup are sealed.
  - `export`: `export --verify` of the scenario's collection, from the
    restored store, must write out every object the backup held, byte
    for byte, each verified against the checksums the catalog records,
    and not the object written after the backup.
  - `served`: the origin, started again on the restored catalog, given
    back to whoever owns the master key, must serve the five objects,
    byte for byte, and answer 404 for the one written after the backup.
    If it does not, the catalog set aside is put back, so that the rest
    of the run finds the store as it was.

  The scenario needs the `pelican-server` that has the CLI, and skips
  without it; the block files that the late object left behind are the
  janitor's to reclaim. It runs between `backups`'s `startup` and its
  later cases, which wait for the schedule to run on past the stop and
  the start, and before `capacity`, which would leave the store too
  full for its objects.

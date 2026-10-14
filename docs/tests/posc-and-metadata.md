# POSC and metadata

Part of [What the tests check](README.md).

POSC and metadata are features of the `posixv2` origin, which is the
default, and XRootD's POSC of its `posix` origin; `fed.sh` refuses each
with any other. The `posc` and `metadata` suites run only where they
apply, and skip otherwise; `./fed.sh test -l posc metadata` lists their
scenarios.

- **POSC** (`-p origin-posc`): the origin stages each upload in
  `<store>/.pelican-posc/` and renames it into place once it completes.
  The `posc` suite interrupts uploads in several ways and checks what the
  origin answers and what the store shows. It judges an interruption
  only once it has seen the upload under way, from a new staging file;
  an upload refused, or unanswered, before then fails. While an
  overwrite is under way, the origin must still serve the old object;
  and of two whole uploads of one object at once (`concurrent`), the
  object must end up all one of them. `hidden` lists the export while an
  upload is staged, so that `.pelican-posc` is there to hide: by
  PROPFIND, as `pelican object ls` does, and as a browser does, by a
  GET for HTML, whose links it reads. Both must list `data/`, and
  neither may show `.pelican-posc`. Pelican hides the staging directory
  from PROPFIND, since POSC's filesystem says it isn't there, but its
  HTML listing reads the export's directory itself
  (`streamDirectoryListing` in `origin_serve/handlers.go`) and shows
  it, so the row fails there, on main and v26.0.0-rc.3 alike.

  `posc-expiry` checks, at the first origin, what POSC does with
  abandoned uploads (`expireFiles` and `touchOpenFiles` in
  `origin_serve/posc.go`), and how it guards its staging directory:

  - `mode`: a complete upload's object must have mode 0644, as without
    POSC: the 0666 with which the WebDAV handler opens a PUT's file,
    less the origin's umask, 022. POSC opens its staging file with the
    mode asked for. This upload also makes the staging directory, if
    there is none yet; the suite never makes it.
  - `stale`: a staging file that the suite plants in a staging
    directory of its own, `.pelican-posc/expiry-<run>/in_progress.*`,
    last modified two hours ago, past the default
    `Origin.Posc.FileTimeout` of an hour, must be removed within 15
    seconds, since the reaper sweeps every 5, and counted in
    `pelican_origin_posc_expired_total`.
  - `staging-put` and `staging-mkcol`: a PUT and a MKCOL straight into
    `.pelican-posc/` must be refused, the PUT with 409, as for a path
    whose parent is missing, since POSC hides the directory, and the
    MKCOL with 405, since POSC refuses to make anything there; and
    neither may leave anything there.
  - `idle`: the suite restarts the first origin with
    `Origin.Posc.FileTimeout` at 20 seconds and `KeepaliveInterval` at
    5, as the last of its configuration layers
    ([service control](../../README.md#service-control)), and restores it
    afterward, after which the federation must serve again within 150
    seconds. A staging file planted 40 seconds old must then be reaped,
    which shows the short timeout in force. An upload that sends half
    of its 2 MiB, then idles 35 seconds, past the timeout, must keep
    its staging file, whose mtime POSC's keepalive refreshes, and
    complete, whole, once it sends the rest. The other scenarios run
    before the restart, under the default timeout. The row needs a
    control helper and a topology other than `tiny`; elsewhere, it
    skips, and says why.
- **XRootD's POSC** (`-p origin-atomic-uploads`): `Origin.EnableAtomicUploads`
  stages each upload in `Origin.UploadTempLocation`, which Pelican
  requires on the exports' filesystem but in none of them, so `fed.sh`
  puts it beside their storage in each origin's store,
  `framework/var/data/origin/<N>/.in-progress/`. The `posc` suite runs
  the same interruptions, judged by the staging files there and by the
  store. `hidden` skips, since nothing is staged inside an export, as
  does `posc-expiry`, since XRootD reaps its own staging; and `stalled`
  checks no metric, which only the native POSC has.
- **pstore** (`-p origin-pstore`) commits a new version of an object only
  when its upload completes, so the `posc` suite runs there too: the same
  interruptions, judged by what the origins serve, once
  `pstore/objects` has grown to show the upload under way (`chunked`
  sends just over pstore's 1 MiB spill threshold for this). `stalled`,
  `hidden`, and `posc-expiry`, which are about POSC's staging, skip.
- **Metadata** (`-p origin-metadata`): the origins send an event for every
  object committed, overwritten, or deleted to the `metadata` recorder. It
  saves each request in `framework/var/data/metadata/` (listed in
  `index.tsv`) and passes it to `metadata-verifier`: Pelican's
  `sample_metadata_server`, built from `PELICAN_TAG`'s source. The recorder
  answers 503 once for objects named `fail-once-*`, 503 always for
  `fail-always-*`, which holds an event queued, and 422 for `reject-*`.
  It reads `recorder.py` when it starts, so after the framework changes
  it, run `./fed.sh restart metadata`.
  The `metadata` suite uploads with `--metadata-file` and
  `--metadata-body`, and checks the events, including that
  `object.updated` carries the new size and custom fields, compared
  type for type. Beyond the events of uploads, it checks, at the first
  origin:

  - `status-urls`: an eventual-mode upload whose event outlives it
    hands the uploader two URLs, `X-Pelican-Metadata-Query-Url` and
    `X-Pelican-Metadata-Manage-Url`, on the origin's web API, which
    take no token: the random token in each is the capability
    (`origin_serve/metadata_status_api.go`). `queued`: an upload of a
    `fail-once-*` object must answer `queued`, with both URLs, each of
    which a GET must answer with the event, `pending`; once the event
    is delivered, which deletes it, both must answer 404. `rejected`:
    an upload of a `reject-*` object must answer `rejected`, with both
    URLs, which must read it `rejected`, as it stays; a DELETE through
    the manage URL must remove it (200), after which both answer 404.
    `cancel`: for an upload of a `fail-always-*` object, a DELETE
    through the query URL, which may only read, must get 403, and leave
    the event; one through the manage URL must cancel it (200); then
    both URLs must answer 404, and for 20 seconds, though a retry was
    due within 10, the recorder must get no delivery of it.
  - `catalog`: the events of changes to the store other than an upload,
    each awaited for 30 seconds. `move`: a MOVE of an object must send
    `object.committed` (or `object.updated`) for its new path, and
    `object.deleted` for its old one. `copy`: a COPY of an object
    uploaded with custom fields must send `object.committed` for the
    copy, with the source's fields. `collection-delete`: a DELETE of a
    collection that holds two objects must send `object.deleted` for
    each. Pelican records a MOVE in its record of objects, but
    publishes nothing for it (`aferoFileSystem.Rename` in
    `origin_serve/filesystem.go`); removes a collection with one
    `RemoveAll`, for which it publishes `object.deleted` for the
    collection's own path, and none for its objects; and publishes the
    copy as an upload of it with no custom fields, since a COPY carries
    none. So the three rows fail, on main and v26.0.0-rc.3 alike.
    Whether a copy's event should carry its source's fields is not
    settled; the suite holds that the catalog should describe the copy
    as it does the source.

    The reconcile sweep (`Origin.Metadata.ReconcileEnabled`, on by
    default) re-enqueues an object whose publish was lost, as to a
    crash between the commit and the queue's write, and, its
    documentation says, never touches an object the origin did not
    write. The suite makes three objects that it must leave alone:
    `reconcile-cancelled`, an upload of a `fail-always-*` object whose
    event is cancelled through its manage URL; `reconcile-rejected`, an
    upload of a `reject-*` object; and `reconcile-observed`, an object
    written straight to the store, which the origin then serves, and so
    records as one it did not write. Each must be in the origin's
    record of objects, or its row is inconclusive. Then the suite
    restarts the first origin with a sweep every 15 seconds, of objects
    at rest for 5 (`ReconcileInterval` and `ReconcileSettleWindow`, 2h
    and 6h by default), and for 45 seconds the recorder must get no
    delivery of any of the three. Pelican's sweep takes every live
    record without a publish of its ETag and with no pending queue row
    (`ReconcileCandidates` in `origin_serve/object_metadata.go`), so all
    three rows are expected to fail: the rejected event is republished
    at every sweep, and every object that the origin has served but not
    written is published, as if it had. The rows run in eventual mode
    only.
  - `object-metadata-cli`: the first origin's record of objects
    (`Origin.Metadata.TrackAccess`, which the metadata presets turn on).
    `api`: after an upload, each of its admin routes,
    `/api/v1.0/origin_ui/object_metadata`, `/lookup`, and `/history`,
    must refuse no credentials (401) and an ordinary user (403); the
    admin's lookup must have the object, of its size, and its history a
    `commit`. The ordinary user, `test-metadata`, is made as the
    `webapi` suite's is ([web API](web-api.md)). `cli`:
    `pelican-server origin object-metadata list`, `get`, and `history`,
    with the admin's token file, must each print, as JSON, what the
    route it asks answers the admin, but for access times. It needs a
    `pelican-server`, as `collections`'s `cli` does
    ([client binary](../../README.md#client-binary)), and skips without
    one.
  - `queue-admin`: the publish queue's admin routes,
    `/api/v1.0/origin_ui/metadata_queue` (its list and `/_health`, and an
    event's GET, DELETE, and `/retry`), with an event held queued: the
    suite writes a `fail-always-*` object straight to the store, and
    deletes it through the origin, which then publishes
    `object.deleted`, in either mode. `auth`: each route must refuse no
    credentials (401) and an ordinary user (403). `admin`: the admin's
    list must have the event, `/_health` count it, and a GET show it; a
    GET of an event that isn't there must get 404. `retry`: with the
    event's next delivery minutes away, a retry must answer 200, and the
    recorder get the event again within 30 seconds. Pelican's handler
    moves the event's next attempt to now, but wakes no worker
    (`retryQueueRowHandler` in `origin_serve/metadata_admin.go`), and
    the workers sleep until the attempt they last saw due, so the row
    is expected to fail. `delete`: the admin's DELETE must remove the
    event (204). With publishing off, `queue-admin` runs alone, as one
    row, `off`: the list must refuse no credentials (401), and answer
    the admin, as `/_health` must, 503.
  - `backpressure` (eventual mode): the namespace's queue capped, at two
    rows more than it has pending when the suite asks
    (`Origin.Metadata.MaxQueuedPerNamespace`), and at 4096 bytes of
    blobs (`MaxQueuedBytesPerNamespace`); both are off by default.
    `rows`: uploads of `fail-always-*` objects, each of which must be
    taken, until the namespace has as many rows pending as the cap; then
    an ordinary upload must be refused with a 5xx, which tells a client
    to try again later, leave nothing, and be counted in
    `pelican_origin_metadata_backpressure_refusals_total{limit="rows"}`.
    `bytes`: with those rows deleted through the admin API, an upload
    of a `fail-always-*` object with a 4096-byte blob (a multipart PUT,
    as in [exports and copies](exports-and-copies.md)) must be taken; then
    an ordinary upload must be refused, as before, and counted for
    `bytes`. `drained`: with the held rows deleted, an ordinary upload
    must be taken, and stored. Pelican refuses an upload by failing its
    close (`ErrMetadataBacklogFull`), which x/net's WebDAV handler
    answers with 405, so `rows` and `bytes` fail, on main and
    v26.0.0-rc.3 alike; and without POSC (`-p origin-posc`), nothing
    removes the refused object, so they fail for that too.

  `queue-admin` and `backpressure` restart the first origin together,
  with every failed delivery put off for ten minutes
  (`Origin.Metadata.MinBackoff` and `MaxBackoff`), so that a held event
  stays put, and with the caps; `catalog`'s reconcile rows restart it
  again. Each restart's configuration is the last of the origin's
  layers, and is removed afterward, after which the federation must
  serve again within 150 seconds
  ([service control](../../README.md#service-control)). Without a control
  helper, or in the `tiny` topology, the restarts' rows skip, but for
  `queue-admin`'s, which run at the origin as it is, but for `retry`.
  Afterward, the suite deletes through the origin the objects whose
  events the recorder always fails, so that the origin records them
  gone, and then removes the run's rows from the queue.
- **Transactional mode** (`-p origin-metadata-tx`): an upload should fail
  with a 5xx if its event can't be delivered, and its object should be
  removed, as Pelican's `docs/metadata-publish-design.md` says.
  The `metadata` suite's `retry` and `reject` check both, and that the
  recorder answered a delivery for the object with 503 or 422.
  `status-urls`, `catalog`'s reconcile rows, and `backpressure` skip,
  since a transactional event never outlives its upload; the rest run.

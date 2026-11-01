# POSC and metadata

Part of [What the tests check](README.md).

POSC (persist on successful close) and metadata publishing are features of
the `posixv2` origin, the default
([`-p origin-posixv2`](../../framework/presets/origin-posixv2.sh); Pelican's
[`docs/v2-origin-posc-and-metadata.md`](https://github.com/PelicanPlatform/pelican/blob/main/docs/v2-origin-posc-and-metadata.md)
and
[`docs/metadata-publish-design.md`](https://github.com/PelicanPlatform/pelican/blob/main/docs/metadata-publish-design.md));
[`fed.sh`](../../fed.sh) refuses either with another origin. A
[case](../glossary.md#case)'s row is named `<scenario> <case>`.

## posc

The [`posc`](../../framework/suites/posc.py) suite interrupts uploads to the
first origin, or races two, and checks what the origin answers, serves
meanwhile, and keeps. It runs wherever `/protected-a` takes writes, and
judges each origin by its staging: POSC
([`-p origin-posc`](../../framework/presets/origin-posc.sh)),
[XRootD](../glossary.md#native-and-xrootd)'s atomic uploads
([`-p origin-atomic-uploads`](../../framework/presets/origin-atomic-uploads.sh)),
pstore ([`-p origin-pstore`](../../framework/presets/origin-pstore.sh)), or
none. An interruption is judged once the upload is seen under way; an upload
refused or unanswered before then fails. An interrupted upload must not be
answered with a success or 405.

- `commit`: A complete 1 MiB upload must land whole, leaving no staging
  file.
- `stalled`: While an upload stalls, no object may be seen, in the store or
  by a HEAD. Under POSC, `pelican_origin_posc_active_uploads` must count it.
  Skips on pstore.
- `sized`, `chunked`: An upload cut short of its `Content-Length`, or in
  chunks, must leave no object
  ([RFC 9110, section 8.6](https://www.rfc-editor.org/rfc/rfc9110#section-8.6)).
- `overwrite`: During an overwrite, and once it is cut short, the origin
  must serve the old object intact.
- `concurrent`: Of two uploads of one object at once, the object must end up
  all of one that succeeded.
- `client`: Killing the [Pelican client](../glossary.md#pelican-client)'s
  `pelican object put` midway through a 1 GiB upload must leave nothing.
- `hidden`: While an upload is staged, neither a Depth 1 PROPFIND of the
  export nor an HTML listing may show `.pelican-posc`. POSC only.
- `posc-expiry`, at the first origin, POSC only:
  - `mode`: A complete upload's object must have mode 0644, as without POSC.
  - `stale`: A staging file older than `Origin.Posc.FileTimeout` must be
    removed within 15 seconds, and counted in
    `pelican_origin_posc_expired_total`.
  - `staging-put`, `staging-mkcol`: A PUT into `.pelican-posc/` must get
    409, and a MKCOL 405, leaving nothing.
  - `idle`: With the first origin restarted with `Origin.Posc.FileTimeout`
    at 20 seconds and `KeepaliveInterval` at 5
    ([service control](../../README.md#service-control)), a staging file 40
    seconds old must be reaped, and an upload that idles 35 seconds midway
    must keep its staging file and complete whole. Needs a
    [control helper](../glossary.md#service-control-helper) and a
    [topology](../glossary.md#topology) other than
    [`tiny`](../../framework/presets/topo-tiny.sh).

## metadata

Under [`-p origin-metadata`](../../framework/presets/origin-metadata.sh)
(eventual mode) or
[`-p origin-metadata-tx`](../../framework/presets/origin-metadata-tx.sh)
(transactional mode), origins send the `metadata`
[helper service](../glossary.md#helper-service), a
[recorder](../../framework/metadata-server/recorder.py), an event for each
object committed, overwritten, or deleted. The recorder logs each in
`framework/var/data/metadata/`, and passes it to `metadata-verifier`,
Pelican's
[`sample_metadata_server`](https://github.com/PelicanPlatform/pelican/tree/main/cmd/sample_metadata_server).
It answers 503 once for objects named `fail-once-*`, 503 always for
`fail-always-*`, and 422 for `reject-*`.

In transactional mode, the [`metadata`](../../framework/suites/metadata.py)
suite's `status-urls`, `catalog`'s reconcile rows, and `backpressure` skip.
With publishing off, only `queue-admin off` runs. `object-metadata-cli` and
`queue-admin` run in the [WLCG pass](../glossary.md#pass) only
([token profiles](README.md#token-profiles)).

`queue-admin`, `backpressure`, and `catalog`'s reconcile rows restart the
first origin with configuration of their own, removed afterward. Without a
control helper, or under `tiny`, their rows skip, but for `queue-admin`'s
other than `retry`.

- `fields`: `pelican object put --metadata-file` must send
  `object.committed` with the file's custom fields, type for type.
- `blob`: With `--metadata-body` too, the event must carry the blob.
- `update`: An overwrite must send `object.updated`, with the new size,
  ETag, and custom fields.
- `delete`: A DELETE must send `object.deleted`.
- `status`: An upload must get `X-Pelican-Metadata-Status: published`.
- `retry`, `reject`: An upload whose event the recorder fails once, or
  rejects. In eventual mode, the upload must succeed, `queued` or
  `rejected`, and `retry`'s event then be delivered. In transactional mode,
  it must fail with a 5xx, its object removed.
- `status-urls`: An upload whose event outlives it must get
  `X-Pelican-Metadata-Query-Url` and `X-Pelican-Metadata-Manage-Url`, which
  need no token:
  - `queued`: For a `fail-once-*` upload, both must show the event
    `pending`, then answer 404 once it is delivered.
  - `rejected`: For a `reject-*` upload, both must show it `rejected` until
    a DELETE through the manage URL removes it.
  - `cancel`: For a `fail-always-*` upload, a DELETE through the query URL
    must get 403; one through the manage URL must cancel the event. Both
    must then answer 404, and the recorder get no delivery.
- `catalog`: Events of changes other than uploads, each within 30 seconds:
  - `move`: A MOVE must send `object.committed` (or `object.updated`) for
    the new path, and `object.deleted` for the old.
  - `copy`: A COPY must send `object.committed` with the source's custom
    fields.
  - `collection-delete`: A DELETE of a collection must send `object.deleted`
    for each object in it.
  - `reconcile-cancelled`, `reconcile-rejected`, `reconcile-observed`: The
    reconcile sweep (`Origin.Metadata.ReconcileEnabled`), run every 15
    seconds, must not publish, for 45 seconds, a cancelled event, a rejected
    one, or an object written straight to the store (Pelican's
    [`docs/parameters.yaml`](https://github.com/PelicanPlatform/pelican/blob/main/docs/parameters.yaml)).
- `object-metadata-cli`: The first origin's record of objects
  (`Origin.Metadata.TrackAccess`):
  - `api`: Its admin routes, `/api/v1.0/origin_ui/object_metadata`,
    `/lookup`, and `/history`, must refuse no credentials (401) and an
    ordinary user (403), and show the admin an upload's object and its
    `commit`.
  - `cli`: `pelican-server origin object-metadata list`, `get`, and
    `history` must each print what its route answers the admin. Skips
    without `pelican-server`
    ([client binary](../../README.md#client-binary)).
- `queue-admin`: The publish queue's admin routes,
  `/api/v1.0/origin_ui/metadata_queue`, with a `fail-always-*` event held:
  - `auth`: Each must refuse no credentials (401) and an ordinary user
    (403).
  - `admin`: The list must have the event, `/_health` count it, and a GET of
    it show it; a GET of a missing event must get 404.
  - `retry`: With the next delivery put off ten minutes
    (`Origin.Metadata.MinBackoff`, `MaxBackoff`), a retry must answer 200,
    and the recorder get the event within 30 seconds.
  - `delete`: A DELETE must remove the event.
  - `off`: With publishing off, the list and `/_health` must answer the
    admin 503, and refuse no credentials (401).
- `backpressure`: The namespace's queue capped at two more rows than it
  holds (`Origin.Metadata.MaxQueuedPerNamespace`), and at 4096 bytes of
  blobs (`MaxQueuedBytesPerNamespace`):
  - `rows`, `bytes`: Uploads of `fail-always-*` objects up to each cap must
    be taken. Past it, an upload must be refused with a 5xx, leaving
    nothing, and counted in
    `pelican_origin_metadata_backpressure_refusals_total` under its `limit`.
  - `drained`: With the queue emptied, an upload must be taken.

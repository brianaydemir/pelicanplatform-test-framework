# Exports and copies

Part of [What the tests check](README.md).

Each origin export has its own capabilities (`Capabilities` in
`Origin.Exports`), among them `Listings`, `DirectReads`, and `Copies`, which
`Writes` implies unless `Origin.DisableCopies` is on (Pelican's
[`docs/parameters.yaml`](https://github.com/PelicanPlatform/pelican/blob/main/docs/parameters.yaml)).
The [`exports`](../../framework/suites/exports.py) suite checks that each
[native](../glossary.md#native-and-xrootd) origin enforces each export's
own, along with third-party COPY and multipart PUTs. XRootD origins are
asked where noted.

Every [scenario](../glossary.md#scenario) but `nested-export` needs
`/protected-a` to take writes from direct clients, and skips without.
`sentinel` runs in the [WLCG pass](../glossary.md#pass) only
([token profiles](README.md#token-profiles)). A
[case](../glossary.md#case)'s row is named `<scenario> <case>`.

Several scenarios restart origins with configuration of their own, as the
last of their [configuration layers](../glossary.md#configuration-layer),
and restore them afterward
([service control](../../README.md#service-control)). Within 150 seconds of
a restart, the federation must serve again, or the restart's rows fail.
Restarts need a [control helper](../glossary.md#service-control-helper) and
a [topology](../glossary.md#topology) other than
[`tiny`](../../framework/presets/topo-tiny.sh); elsewhere their rows skip.

- The capability restart: every origin, with `/protected-b` (or
  `/protected-a`) without `Listings`, `/public` without `DirectReads`, and
  `Origin.DisableCopies` on. Needs native origins that take direct clients.

- The nested restart: every origin, XRootD's too, with an export nested in
  `/public`.

- The gate restart: every `https` and `httpsv2` origin, reaching its backend
  through the `webdav-gate` [helper service](../glossary.md#helper-service)
  (`Origin.HttpServiceUrl`), which logs each request's `Authorization`.

- The sentinel restart: the first origin, with a `SentinelLocation` for
  every export.

The scenarios:

- `multipart-put`: With metadata off, a `multipart/form-data` PUT must be
  stored as sent
  ([RFC 9110, section 9.3.4](https://www.rfc-editor.org/rfc/rfc9110#section-9.3.4));
  `Origin.Metadata.AllowMultipart` is for metadata publishing alone. At each
  origin, XRootD's too: `pelican-shaped` (a `metadata` part, then `object`)
  and `form-shaped` (one `file` part). Skips under
  [`-p origin-metadata`](../../framework/presets/origin-metadata.sh) and
  [`-p origin-metadata-tx`](../../framework/presets/origin-metadata-tx.sh).

- `tpc-body`: At each native origin, a pull-mode COPY
  ([WLCG HTTP-TPC](https://twiki.cern.ch/twiki/bin/view/LCG/HttpTpc)) of an
  8 MiB object, with the source's token in `TransferHeaderAuthorization`,
  must get 201 and Perf Markers, each ending `End`, whose
  `Stripe Bytes Transferred` never falls, ending at the size, then
  `success: Created`, and leave the object at the destination.

- `tpc-failures`: COPYs that must fail, writing nothing:

  - `bad-source`: a `Source` that is no http or https URL: 400.
  - `source-error`: a source that answers 404 or 500: 502.
  - `missing-source`: an object no origin holds, by an error status or a
    last line of `failure: ...`; `pelican object copy` must exit non-zero.
  - `mid-stream`: over an object X, from a source that sends 1 MiB of a
    claimed 4 MiB. Where uploads are staged
    ([`-p origin-posc`](../../framework/presets/origin-posc.sh), or
    [pstore](../../framework/presets/origin-pstore.sh)), X must be
    unchanged; elsewhere it may be gone, but not altered. Under
    `-p origin-metadata`, an origin that kept X must publish no
    `object.deleted` for it.

- `disable-copies`:

  - `public`: A COPY to `/public`, which takes no writes, must be refused
    (401, 403, or 405). XRootD origins are asked too. Skips under
    [`-p origin-public-writes`](../../framework/presets/origin-public-writes.sh).
  - `disabled`: Under the capability restart, a COPY to `/protected-a`: 405
    ([RFC 9110](https://www.rfc-editor.org/rfc/rfc9110#section-15.5.6)). An
    OPTIONS of `/` with no token: 200, with an `Allow` that names
    no COPY.

- `listings-capability`: Under the capability restart, in the export without
  `Listings`, a Depth 1 PROPFIND of a collection, and a GET of it for HTML,
  must be refused (401, 403, or 405); a Depth 0 PROPFIND of an object must
  get 207. Where that export is not `/protected-a`, a Depth 1 PROPFIND in
  `/protected-a` must get 207. The directors' part is the
  [`listings`](listings.md) suite's.

- `direct-reads-capability`: Under the capability restart, a direct GET of
  an object in `/public` must be refused (401 or 403), and one in
  `/protected-a` served. Each federated cache must serve the first. Skips
  where `/public` is not exported.

- `nested-export`: Under the nested restart, which adds
  `/public/data/exports/<run>/secret` with `/protected-a`'s capabilities,
  the longest prefix must pick a path's export (compare
  [another owner](another-owner.md)). At each origin, a GET:

  - `served`: of an object beside the export, with no token: served;
  - `token`: of an object in it, with a read token for it: served;
  - `wrong-scope`: with a token scoped to `/elsewhere/`: refused (401 or
    403);
  - `protected`: with no token: refused.

  And with the read token, by every [route](../glossary.md#route) (through
  the director's choice, direct, and from each federated cache, named):

  - `client`: `pelican object get` must get the object;
  - `plugin`: so must [`stash_plugin`](../glossary.md#stash_plugin), with
    the token alone in its credentials directory.

  The Pelican clients present a token for a read only where the director
  says that the namespace takes one, as it must of the nested export.

  Needs `/public` exported, and origins that take direct clients.

- `backend-token`: Under the gate restart, with a static token
  (`Origin.HttpAuthTokenFile`), PUT, GET, HEAD, PROPFIND, and at native
  origins MKCOL, MOVE, and DELETE must work at each origin, and so must the
  [Pelican client](../glossary.md#pelican-client)'s `pelican object get` and
  `put`. Every request the gate logged must carry the static token, and no
  client's.

- `backend-passthrough`: The same, forwarding the client's token
  (`Origin.HttpAuthTokenPassthrough`), at `httpsv2` origins only. Each
  request for a path the test asked an origin for must carry the client's
  token; none may carry the static one.

- `sentinel`: Under the sentinel restart, with every export's
  `SentinelLocation` file in place, the origin must start and serve
  (`present`); with one missing, its container must exit within 120 seconds
  (`missing`). Where `present` fails, `missing` is not run.

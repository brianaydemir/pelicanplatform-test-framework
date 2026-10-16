# Exports and copies

Part of [What the tests check](README.md).

Each export of an origin has capabilities of its own (`Origin.Exports`):
`Listings`, `DirectReads`, and `Copies`, which follows `Writes` unless
`Origin.DisableCopies` turns it off (`server_utils/origin.go` in
Pelican). An origin must enforce each export's own, and not only the
switches that apply to every export. The `exports` suite checks this at
each native origin, along with a third-party COPY, which a native origin
serves itself (`origin_serve/tpc_handler.go`), and a PUT of a multipart
body. Its objects go under `/<namespace>/data/exports/<run>/`, which it
removes afterward. A COPY's failing sources are a stub HTTP server in
the dev container (`framework/testlib/stub.py`), which the origins reach
at `http://dev:<port>/`. A scenario with several cases records a row per
case, named `<scenario> <case>`.

No shape gives an export without `Listings`, or one without
`DirectReads` beside others with them, or turns copies off. So, for
`listings-capability`, `direct-reads-capability`, and `disable-copies
disabled`, the suite restarts every origin with `fed.sh`'s
`Origin.Exports` restated, as the last of its configuration layers
([service control](../../README.md#service-control)): `/protected-b` (or,
where it is not exported, `/protected-a`) without `Listings`; `/public`,
if it is exported, without `DirectReads`; and `Origin.DisableCopies` on.
Afterward it restores them. Before those rows, and after, every origin
must serve a probe object, every director name every origin for it,
and every federated cache serve it, within 150 seconds. The restart
needs native origins that take direct clients, since Pelican refuses
`DirectReads` and `Listings` with `Origin.DisableDirectClients`
(`-p origin-no-direct`); a control helper; and a topology other than
`tiny`, whose origin is the whole federation's process. Elsewhere, those
rows skip, and say why. Nor does any shape give one origin an export
nested in another, so `nested-export` restarts every origin once more,
XRootD's too, with one, and restores them likewise.

An `https` or `httpsv2` origin may have to present a token to its
backend (`Origin.HttpAuthTokenFile`, a static one on every request;
or, for `httpsv2`, `Origin.HttpAuthTokenPassthrough`, the client's own),
and the `webdav` service asks for none, so nothing else would show
what an origin sends. The `webdav-gate` service, nginx in front of it,
relays every request and logs the `Authorization` it came with, in
`framework/var/data/webdav-gate/access.log`, and checks nothing: the
log is the evidence. `backend-token` and `backend-passthrough` restart
every origin to reach its backend through the gate
(`Origin.HttpServiceUrl`), so configured, in the tests' own layer, and
restore them afterward; the static token is one `fed.sh init` made, in
`framework/var/generated/http-auth-token`. They need `/protected-a` to
take writes and direct clients, and a control helper; elsewhere they
skip.

- `multipart-put`: with metadata off, a PUT typed `multipart/form-data;
  boundary=...` must be stored as sent, as a PUT stores its body (RFC
  9110, section 9.3.4). At each origin that takes direct writes, a PUT
  of a well-formed multipart document, then a GET of it, must get it
  byte for byte, as must the store: `pelican-shaped`, a `metadata` part
  and then an `object` part, as Pelican's metadata uploads are; and
  `form-shaped`, one `file` part, as an HTML form sends. A native
  origin takes every such PUT apart (`rewriteMultipartPUT` in
  `origin_serve/metadata_multipart.go`), gated by
  `Origin.Metadata.AllowMultipart`, which is on by default, and not by
  `Origin.Metadata.Enabled`: it stores only the first's `object` part,
  and refuses the second (400), so both rows fail there, on main and
  v26.0.0-rc.3 alike. An XRootD origin stores both as sent, and is the
  control. Under `-p origin-metadata`, the parts are taken apart by
  design, and the rows skip.
- `tpc-body`: at each native origin, a third-party COPY (WLCG HTTP-TPC,
  pull mode) of an 8 MiB object in `/protected-a`, whose `Source` is
  the first federated cache (or, without one, another origin, or the
  origin itself), with the `server` token for the destination, and for
  the source in `TransferHeaderAuthorization`, which the origin sends
  the source as `Authorization`. It must get 201, and an answer of
  Perf Markers, each ending `End`, whose `Stripe Bytes Transferred`
  never falls nor exceeds the object, the last of them the object's
  size, then a last line of `success: Created` (Pelican's
  `TestMonitorTPC`). The destination must then hold the object. The
  row's note counts the markers.
- `tpc-failures`, at each native origin, a COPY to a path of its own:
  `bad-source`, with a `Source` that is no http or https URL (`ftp:`,
  and `file:///etc/passwd`), must get 400; `source-error`, from the stub
  answering 404, and 500, must get 502; and `missing-source`, from the
  cache (as `tpc-body`) of an object no origin holds, must fail, by an
  error status or a last line of `failure: ...`. None may write the
  destination; and `pelican object copy` of a missing object must exit
  non-zero, and leave no origin holding its destination. Where an
  origin never asked the stub, a 502 shows nothing, and the row is
  inconclusive. `mid-stream`: over an object X, a COPY from the stub
  claiming a `Content-Length` of 4 MiB and sending 1 MiB must fail, and
  then, where uploads are staged (POSC, `-p origin-posc`, or a `pstore`
  origin), leave X as it was. Elsewhere, the origin opens its
  destination truncated, and so has lost X before it learns that the
  copy fails; X intact or gone is accepted there, never other bytes,
  and the row's note says which. Pelican aborts a staged upload, which
  keeps X, but then removes the destination anyway (`RemoveAll` in
  `handleCopyTPC`), so the row fails under `-p origin-posc` and
  `-p origin-pstore`, on main and v26.0.0-rc.3 alike. Under
  `-p origin-metadata`, an origin that kept X must publish no
  `object.deleted` for it within 15 seconds.
- `disable-copies`: `public`, in the shape as it is, a COPY to
  `/public`, which takes no writes, and so has no `Copies`, from the
  probe at the origin itself, must be refused (404 or 405, as a native
  origin routes no COPY there, or 401 or 403), and write nothing. Every
  origin is asked, an XRootD one too; where `/public` takes writes
  (`-p origin-public-writes`), and so copies, the row skips. `disabled`,
  under
  `Origin.DisableCopies`: at each native origin, a COPY to
  `/protected-a` must get 404 or 405, since no export then has
  `Copies`, and a native origin routes COPY only for an export with it
  (`RegisterHandlers` in `origin_serve/handlers.go`), and write nothing;
  and OPTIONS of the server's root, with no token, must answer 200,
  with an `Allow` that names no COPY (`registerRootOptions`; Pelican's
  `TestRootOptionsAdvertisesCopyOnlyWhenEnabled`).
- `listings-capability`: of the export without `Listings`, with its
  `server` token, at each native origin, a PROPFIND of Depth 1 of a
  collection, and a GET of it with `Accept: text/html`, must be refused
  (401, 403, or 405); a PROPFIND of Depth 0 of the object in it, a
  stat, must answer 207, with its size. Where that export is not
  `/protected-a`, a PROPFIND of Depth 1 in `/protected-a`, which keeps
  `Listings`, must answer 207, so that the refusal is the export's
  alone. The directors already send no deeper
  PROPFIND to an export without `Listings` (`director/sort.go`), which
  the `listings` suite checks; this is the origin's own check. Nothing
  in a native origin reads `Listings` (its ACLs come from
  `PublicReads`, `Reads`, and `Writes` alone, in
  `origin_serve/authz.go`), so the row fails there, on main and
  v26.0.0-rc.3 alike.
- `direct-reads-capability`: with `/public` without `DirectReads`, while
  `/protected-a` keeps them, a GET of an object in `/public` straight to
  each native origin must be refused (401 or 403), with none of its
  bytes, while one of the probe in `/protected-a` is served; and a GET
  of it through each federated cache must be served. A native origin
  checks `DirectReads` only through `Origin.DisableDirectClients`, for
  every export at once (`origin_serve/handlers.go`), so the row fails
  there, on main and v26.0.0-rc.3 alike. It skips where `/public` is not
  exported.
- `nested-export`: an operator may nest a protected prefix in a public
  one on one origin, as another owner may register one inside it
  ([another owner](another-owner.md)): Pelican picks a path's export by
  its longest prefix (`LongestNSMatch`), and its native origin's
  authorization speaks of exports nested one inside another
  (`origin_serve/authz.go`). Every origin, XRootD's too, is restarted
  with `/public`'s exports and one more, nested in it,
  `/public/data/exports/<run>/secret`, with `/protected-a`'s
  capabilities, whose storage is that directory of `/public`'s, and
  which the origins register as they start; before the restart, two
  objects go into `/public`'s storage at every store, `open`, beside the
  export, and `secret/obj`, inside it. With the origins restarted, a GET
  of `open` straight to each origin, with no token, must get it, as
  `/public`'s (`served`); a GET of `secret/obj` with a read token for
  the export, from the issuer the origins give it (its own, under the
  origin's `/api/v1.0/issuer/ns`, or the one every namespace has),
  signed with the origins' key, must get it (`token`); with such a token
  scoped to `/elsewhere/`, be refused (401 or 403), without its bytes
  (`wrong-scope`); and with no token, be refused too (`protected`): the
  export takes a token, though it is nested in `/public`, which anyone
  may read. Afterward the origins are restored, and the export's
  registration removed, with a token of the origins' key, as `pelican
  namespace delete` mints one. Pelican's native origin registers a
  route group with a catch-all per export, and its router (gin) refuses
  a catch-all whose prefix holds another route, so an origin with an
  export nested in another panics as it starts, and never serves (O14).
  An XRootD `posix` origin lays each export out as a symbolic link under
  its export root, at the export's prefix (`CheckOriginXrootdEnv` in
  `xrootd/xrootd_config.go`), so a nested export's link would go inside
  the outer export's, where its own storage already is, and the origin
  stops with "file exists". So the restart fails on every origin, and
  every row with it, on v26.0.0-rc.3 and main alike. Were a native
  origin to start, `protected` would fail there too: it grants a
  `PublicReads` export's reads to every path it encloses (`getAcls` in
  `origin_serve/authz.go`), the `auth` suite's `nested-protected` at the
  origin (T4); XRootD's authfile denies the inner prefix outright. It
  needs `/public` exported, origins that take direct clients, a control
  helper, and a topology other than `tiny`; elsewhere it skips, and
  says why. Since no origin starts with the export, the restart's
  failure takes the framework's wait for a restarted server, 180
  seconds, before the origins are restored.
- `backend-token`: with the origins reaching their backend through the
  gate, with the static token, everything must still work, straight at
  each origin with the `server` token: a PUT of an object with a
  `Content-Length` and one in chunks, which the store on disk, which
  the backend serves, must then hold; a GET of the first whole, of a
  range of it, and a HEAD; a PROPFIND of Depth 1 of the run's
  collection, which must list both; and at a native origin, an MKCOL, a
  MOVE of the second object, which the store must show, and a DELETE of
  what it moved. Then, through the federation, `pelican object get` of
  the first object, which a cache, or without one an origin, fetches
  from an origin, and so through the gate; and `pelican object put` of
  a new object. Every request the gate logged meanwhile, the origins'
  own probes of the backend included, must have carried the static
  token, and none a client's, which a backend has no business seeing;
  and the gate must have seen a GET, a PUT, and a DELETE. It runs at
  `https` and `httpsv2` origins.
- `backend-passthrough`: the same with the client's token forwarded
  (`Origin.HttpAuthTokenPassthrough`), at `httpsv2` origins alone, since
  XRootD's HTTPS plugin forwards none. Each request the gate logged for
  a path the test asked an origin for straight must have carried the
  client's token, and none the static one; the rest, the origins' own
  probes, which carry no client's token, and what they fetched for a
  cache, are counted in the row's note.

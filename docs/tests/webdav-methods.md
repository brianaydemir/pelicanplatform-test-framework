# WebDAV methods

Part of [What the tests check](README.md).

Pelican's native origin serves every WebDAV method through
`golang.org/x/net/webdav`, behind its own checks: of the request's
token, for the action it maps the method to (`getActionFromMethod` in
`origin_serve/handlers.go`: GET, HEAD, OPTIONS, and PROPFIND read; PUT,
POST, MKCOL, LOCK, UNLOCK, PROPPATCH, and COPY create; DELETE and MOVE
modify), and of a COPY's or MOVE's `Destination`
(`checkCopyMoveDestination`). The `webdav` suite sends the methods that
no other suite does, straight to each origin in `/protected-a`, under
`/protected-a/data/webdav/<run>/<origin>/`, and to each cache, under
`.../caches/`, and removes what it wrote
afterward. It needs `/protected-a` to take writes and direct clients.
The scenarios that send WebDAV's other methods ask only native origins,
and skip an XRootD origin; `symlink-writes` and `stored-html` ask every
origin, so that an XRootD origin is a control. A scenario's row fails
with the first thing it found wrong; each thing is printed as it is
found. Its tokens are the origins' own, of the scopes each names.

- `post-create-only`: x/net serves a POST as a GET. A POST of an object
  with a token of `storage.create` alone, and with one of
  `storage.read` alone, must each be refused (401 or 403), with none of
  the object's bytes: the first may not read, and a POST is not a write
  anyone asked for. Pelican authorizes a POST as a create, so the first
  gets the object, and the row fails there.
- `copy-move-destination`: COPY and MOVE, with the `server` token. Within
  `/protected-a`, each must succeed (201 or 204), leaving the bytes at
  the destination, and for MOVE, none at the source. To a `Destination`
  in `/protected-b` or `/public`, directly or by climbing out of
  `/protected-a` with `..`, `%2e%2e`, `%2E%2E`, or `.%2e`; or, with a
  token scoped to the run's `a/`, from `a/` to `b/`, each must be
  refused (403), and change nothing at the source or the destination,
  in any store. With no `Destination`, it must get 400, and to another
  server (`https://elsewhere.invalid/...`), 502 (RFC 4918, sections 9.8
  and 9.9), which x/net answers, changing nothing.
- `copy-move-scopes`: with a `storage.create` token, a MOVE must be
  refused (401 or 403), since it removes its source, and change
  nothing; a COPY to a new path must succeed (201); and a COPY over an
  object must be refused, and leave it as it was, since replacing an
  object takes `storage.modify`, as the WLCG token profile says. x/net
  replaces an object that a COPY lands on unless asked not to, while
  Pelican authorizes every COPY as a create (`getActionFromMethod`
  says COPY is purely additive), so that row fails there. Then, with
  the `server` token, a COPY and a MOVE onto an object with
  `Overwrite: F` must each get 412, and change nothing (RFC 4918,
  section 10.6).
- `mkcol`: MKCOL with a `storage.read` token must be refused, and make
  nothing; with the `server` token, it must make a new collection
  (201), which a PROPFIND then shows; where a collection already is, it
  must get 405; and with a body, 415, making nothing (RFC 4918, section
  9.3.1). The row's note says what each origin answered where a
  collection was. Pelican's `posixv2` and `pstore` make a collection
  with `MkdirAll`, and `s3v2` writes a marker, so each answers 201
  there, and the row fails.
- `lock`: Pelican uses no locks, but x/net keeps them, in memory
  (`webdav.NewMemLS()`), forever when a LOCK names no `Timeout`, and
  makes an empty file for a LOCK of a new path, beside POSC and the
  metadata events. With a `storage.create` token and no `Timeout`, a
  LOCK of a new path and one of an object must each be refused, or
  grant a lock with a timeout (`Second-<n>`, with `n` above 0; x/net
  writes a lock without one as `Second-0`); and the new path must hold
  nothing. What locks should do here is not settled, so either is
  accepted. Then, with the `server` token and no lock token, a PUT of
  the new path and a DELETE of the object; the row's note says what
  each got (423, if the lock holds). Each lock granted is then
  released with UNLOCK, which must succeed: otherwise its path stays
  locked until the origin restarts. A native origin grants each lock
  forever, and makes the file, so the row fails there.
- `symlink-writes`: as the `auth` suite's `symlink` rows do for reads,
  it makes symbolic links in `/protected-a`'s storage on disk, to a
  file and a directory beside the namespaces' storage in each store,
  which any uid may write, so that only the server can stop a write
  that follows a link. A HEAD of the linked file with
  `Want-Digest: crc32c` must report no digest of it. A PUT and an MKCOL
  under the linked directory, and a DELETE of the file in it, must be
  refused (4xx). A PUT and a DELETE of the linked file itself may be
  refused, or replace or remove the link alone: a POSC upload renames
  its staged file over the link, and an XRootD origin unlinks it. Each
  must leave the file and the directory as they were, and nothing new
  beside them. Pelican's `posixv2` refuses a link that leaves its
  export (`os.Root`); an XRootD `posix` origin follows it, so the row
  fails there. A `pstore` origin's store is encrypted, and holds no
  links, so it is passed over.
- `stored-html`: an origin serves its data on the host and port of its
  web UI and its login cookie, so an object that a browser renders as a
  page could run script there, against its admin. An HTML page and an
  SVG image, each with a script, put at each origin, must come back
  with `X-Content-Type-Options: nosniff`, and either typed so that a
  browser will not render them (`application/octet-stream`, or
  `text/plain`) or with `Content-Disposition: attachment`. The row's
  note says how each origin typed them. A native origin types an object
  by its name's extension (`http.ServeContent`), and sends no
  `nosniff` (only the V2 cache does), so the row fails there.
- `webdav-semantics`: OPTIONS of each native origin's root, with no
  token, must answer 200, with a `DAV` header and an `Allow` naming
  OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, MKCOL, COPY, and MOVE
  (`registerRootOptions`). A PUT onto a collection must get 405 or 409,
  leaving what it holds. A PROPPATCH of an object, setting two dead
  properties that no server here keeps, must get 207, with a failing
  status for each, or be refused (403, 405, or 501), leaving the
  object as it was. Last, after everything else at every origin, a
  DELETE of `/protected-a`'s root, with the `server` token, must get
  403 or 405, and remove nothing. If an origin honored it, it would
  take `/protected-a`'s storage with it, and `init`'s objects, so
  `webdav` runs after every other suite but `auth`, `transfers`,
  `restart`, and `keys`.
- `cache-methods`: a cache serves objects and listings (GET, HEAD, and
  PROPFIND), and relays a PUT or a DELETE to an origin (`blocks`'s
  `write-through`), and that is all a V2 cache routes
  (`local_cache/persistent_cache_api.go`), so Gin answers every other
  method 405 (`HandleMethodNotAllowed` in `web_ui/ui.go`). Each cache
  is sent POST, COPY, MOVE, PROPPATCH, and LOCK of an object of the
  run's own, which every origin holds, and MKCOL of a new path beside
  it, with the `server` token, which would let an origin honor each: so
  a cache that relayed one would show. A V2 cache must answer 405; an
  XRootD cache must refuse (4xx or 5xx), as its HTTP server sees fit,
  and the row's note says how. Whatever the answer, no origin's store
  may change: the object must keep its bytes, and nothing may appear at
  the new path or at a COPY's or MOVE's `Destination`.

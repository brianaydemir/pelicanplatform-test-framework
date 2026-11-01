# WebDAV methods

Part of [What the tests check](README.md).

The [`webdav`](../../framework/suites/webdav.py) suite sends the
[WebDAV](https://www.rfc-editor.org/rfc/rfc4918) methods that no other suite
does, and checks how each is authorized, in `/protected-a`. It skips where
that namespace takes no writes or direct clients. `symlink-writes` and
`stored-html` ask every origin, with [XRootD](https://xrootd.org/) origins
as controls; the other scenarios skip XRootD origins. Each
[scenario](../glossary.md#scenario) is one row, which fails with the first
problem it finds.

- `post-create-only`: A POST of an object, with a token of `storage.create`
  alone or of `storage.read` alone, must be refused (401 or 403), with none
  of its bytes.

- `copy-move-destination`: With the `server`
  [credential](../glossary.md#credential), a COPY or MOVE within
  `/protected-a` must succeed (201 or 204), moving the bytes. It must be
  refused (403), changing nothing in any store, with a `Destination`:

  - in `/protected-b` or `/public`;
  - that climbs out with `..`, `%2e%2e`, `%2E%2E`, or `.%2e`; or
  - outside the token's scope.

  With no `Destination`, it must get 400, and to another server, 502 (RFC
  4918, sections [9.8](https://www.rfc-editor.org/rfc/rfc4918#section-9.8)
  and [9.9](https://www.rfc-editor.org/rfc/rfc4918#section-9.9)), changing
  nothing.

- `copy-move-scopes`: With a `storage.create` token, a MOVE, and a COPY over
  an object, must be refused (401 or 403), changing nothing; a COPY to a new
  path must succeed (201). With `server`'s, a COPY or MOVE onto an object
  with `Overwrite: F` must get 412
  ([RFC 4918, section 10.6](https://www.rfc-editor.org/rfc/rfc4918#section-10.6)).
  The SciTokens [pass](../glossary.md#pass) checks only `Overwrite: F`.

- `mkcol`: MKCOL with a `storage.read` token must be refused, making
  nothing. With `server`'s, it must make a new collection (201), get 405
  where one is, and get 415 with a body, making nothing
  ([RFC 4918, section 9.3.1](https://www.rfc-editor.org/rfc/rfc4918#section-9.3.1)).

- `lock`: A LOCK with a `storage.create` token and no `Timeout`, of a new
  path or of an object, must be refused or grant a lock with a timeout
  (`Second-<n>`, `n` > 0), creating nothing. The note says what a PUT and a
  DELETE without the lock token then got. UNLOCK must release each lock;
  otherwise, its path stays locked until the origin restarts.

- `symlink-writes`: Through symbolic links in `/protected-a`'s storage to a
  file and a directory outside it, a HEAD with `Want-Digest: crc32c` must
  report no digest; a PUT, MKCOL, or DELETE under the directory must be
  refused (4xx); and a PUT or DELETE of the linked file may be refused, or
  replace or remove the link alone. Nothing outside may change. Skips
  [pstore](pstore.md) origins.

- `stored-html`: An HTML page and an SVG image with scripts, put at each
  origin, must come back with `X-Content-Type-Options: nosniff`, and either
  a type no browser renders (`application/octet-stream` or `text/plain`) or
  `Content-Disposition: attachment`.

- `webdav-semantics`: At each [native](../glossary.md#native-and-xrootd)
  origin:

  - OPTIONS of the root, with no token, must answer 200, with a `DAV` header
    and an `Allow` naming OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, MKCOL,
    COPY, and MOVE;
  - a PUT onto a collection must get 405 or 409;
  - a PROPPATCH of two dead properties must get 207 with a failing status
    for each, or 403, 405, or 501;
  - last, a DELETE of `/protected-a`'s root with the `server` token must get
    403 or 405.

  None may change anything. An origin that honors the DELETE loses
  `/protected-a`'s storage, `init`'s [objects](test-data.md) included.

- `cache-methods`: Each cache, sent POST, COPY, MOVE, PROPPATCH, LOCK, and
  MKCOL with the `server` token, must answer 405 if V2, and refuse (4xx or
  5xx) if XRootD. No origin's store may change. A cache serves only GET,
  HEAD, and PROPFIND, and relays PUT and DELETE
  ([block boundaries](block-boundaries.md)' `write-through`).

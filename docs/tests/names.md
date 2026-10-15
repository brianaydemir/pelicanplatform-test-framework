# Names

Part of [What the tests check](README.md).

The `names` suite puts objects whose names a URL or a listing must
encode under `/protected-a/data/names/<run>/`: a space, `%`, `+`, `#`,
`?`, `&`, `;`, quotes, angle brackets, brackets, `*`, and letters beyond
ASCII; and two that need no encoding but are easily mishandled, a name
that begins with a dot and one of 200 characters. Each sits beside
decoys, whose names a server or client that
mishandles the encoding would take for its own: `x%41y` beside `xAy`,
`a+b` and `a%20b` beside `a b`, `hash#tag` beside `hash`, and
`what?q=1` beside `what`. Every object is a different size. A URL
percent-encodes every character of a name but letters, digits, and
`-._~`, and so does what the client is given.

- A PUT of each name must land in each store on disk as a file of exactly
  that name.
- Every origin and cache must serve each object by its name: any bytes
  but its own fail, and a decoy's are named.
- Every director, asked for each object by its name through its object
  route (`/api/v1.0/director/object/`), must send a plain HTTP client to
  a federated cache, or, with no cache, to an origin, by a redirect
  whose `Location` names the object again. The director takes the name
  decoded and encodes it anew (`getRedirectURL` in Pelican's
  `director/director.go`), so a name it mangled on the way would fetch
  a decoy, or nothing. The client follows the `Location` as it is, as
  `curl -L` does, and must get the object's bytes.
- Every origin and federated cache must list each name once, decoded,
  with its size, as must `pelican object ls`.
- `pelican object get` of every object must write a file of each one's
  name, with its bytes, and nothing else: through the cache the director
  picks, direct from an origin (`--direct`) where `/protected-a` takes
  direct clients, and from each federated cache in turn, named
  (`--cache`), which alone may serve it, since the client builds that
  cache's URL of each name itself; `pelican object put` of a file of
  each name must land as the object of that name.
- Pelican's client fails two of these, on v26.0.0-rc.3 and main alike.
  It reads the director's `Link` header by splitting it at `;`, so a
  URL with `;` in it is cut there (and a character more, for the `>`
  it expects): `semi;colon` is fetched, and put, as `.../sem` with the
  object's path appended, which no server holds, and a `put` lands the
  object under a collection `sem` (L6: `client-get`, `client-put`).
  And `pelican object ls` prints a name as it stands in the listing's
  href, decoded once, but with what a URL may not hold encoded again,
  `%` excepted: `a b` as `a%20b`, like the decoy `a%20b`, and `hash#tag`
  as `hash%23tag` (L7: `client-ls`).
- XRootD mishandles several of these names (XRootD 5.9.7, in the
  v26.0.0-rc.3 images), so where it serves the origins or the caches
  (`-p origin-xrootd`, `-p cache-xrootd`), these rows fail. An XRootD
  origin decodes a PUT's path and then splits it at `?`, so `what?q=1`
  lands as `what`, over the decoy (`origin-put`, `origin-get`,
  `client-put`); its PROPFIND writes each name into its `href` as it
  is, not percent-encoded, so `hash#tag` reads as `hash` with a
  fragment, and `hash` is listed twice (`origin-list`, and `client-ls`
  with it).
  An XRootD cache decodes a name a second time as it asks the origin
  for it: a cold read of `a b` or `hash#tag` gets 403, and one of
  `x%41y` the decoy `xAy`, or 404 (`cache-get`, `director-get`,
  `client-get`); and it lists no collection at all (`cache-list`; see
  [listings](listings.md)).

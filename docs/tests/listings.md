# Listings

Part of [What the tests check](README.md).

The `listings` suite sends PROPFIND straight to every origin, director,
and cache, at depths 0, 1, and infinity, in every namespace, over a
small tree that it uploads under `/<ns>/data/listings/<run>/`. Each
namespace's tree also holds an object only it holds, `only-<ns>`, so
that a server that lists another namespace's lists the wrong tree.
`./fed.sh test -l listings` lists the scenarios.

- An origin must list the tree exactly. It may refuse Depth infinity
  with 403, as RFC 4918 allows, but must not answer it as Depth 1. An
  object's `getlastmodified` must be when its file was written, within a
  minute, where the store is on disk: the origins and the tests keep
  different time zones (`TZ_STORAGE`), so a time rendered in the wrong
  one is hours off.
- A director must redirect a PROPFIND to an origin: Depth 0 (a stat)
  for any namespace that can be read, and deeper only with `Listings`
  (Pelican's `director/sort.go`). The origin's listing must then be
  right; where the namespace takes no direct clients, the origin must
  refuse it instead.
- A cache must answer Depth 0 of anything it may read, and relay deeper
  listings through the director to an origin, listing what the origin
  does. `cache-body` sends the propfind body that `pelican object ls`
  sends, which the cache must pass on as it follows the director's
  redirect: a V2 cache of v26.0.0-rc.3 hands its proxied PROPFIND the
  request's body as a stream, which Go's client cannot send again after
  the director's redirect, so it answers with the director's 307
  instead, and the row fails there (C20; main reads the body first). A
  cache may refuse Depth infinity with 403 only if an origin
  does; where no origin may be asked directly, a 403 is accepted, and
  the row's note says so.
- Deeper than Depth 0, any server, directors included, lists only a
  namespace with `Listings`, and an origin answers only direct clients,
  which a namespace without `DirectReads` has none of. A PROPFIND that may
  not be answered must be refused with 401, 403, or 405.
- Every object in a listing must have a `getcontentlength`, 0 included.
- Listing `/protected-a` with no token, or one scoped elsewhere, must be
  refused everywhere; listing `/public` with none must work.
- `new-object` checks that an object shows up at once, by PROPFIND and
  with `pelican object ls`, and is gone once removed.
- `collection-get` asks every server for a collection by GET, as a naive
  client would, which is no object (Pelican issue #1706): an origin,
  asked without `Accept: text/html`, refuses (a native origin, 405) or
  answers a listing (an XRootD origin, HTML), never an object's bytes,
  and asked with it answers an HTML listing. A federated cache must not
  keep what an origin answers as the collection's object, which every
  later request for that path would then get: its answer must be no
  object's bytes; a V2 cache's 200 must be typed
  `application/octet-stream`, with `X-Content-Type-Options: nosniff`,
  so that a browser never runs a listing as a page on the cache's own
  web origin, and carry no `Age`, on a second GET either, since a V2
  cache sends `Age` only for an object it holds whole; and the object
  in the collection must come back intact afterward.
- `html-links`: a browser user follows the links of an origin's HTML
  listing, so each must lead to its entry. Under
  `/protected-a/data/listings/<run>/html/` are the `names` suite's
  objects, beside their decoys ([names](names.md)), and a collection
  holding one object. A GET of that collection with `Accept: text/html`
  goes to each origin, and each link in its answer is followed as a
  browser follows it: resolved against the listing's URL, without its
  fragment, and with what a URL may not hold as it is percent-encoded,
  but a `%` left as it is. A link is known by its text, the entry's
  name. One to an object must bring its bytes, never a decoy's or a
  404; one to the collection, or to the parent (`..`), if there is one,
  an HTML listing that links to what it holds. Every entry must have a
  link. A native origin's listing must also carry `Cache-Control:
  no-store`, so that no cache keeps it, as Pelican sends it. Pelican's
  native origin writes each link as the entry's path in the export,
  without the namespace, HTML-escaped but not percent-encoded
  (`streamDirectoryListing` in `origin_serve/handlers.go`), so no link
  leads to its entry, and the row fails there, on main and v26.0.0-rc.3
  alike. An XRootD origin writes the whole path, but also not
  percent-encoded, so the links to `a%20b`, `x%41y`, `hash#tag`, and
  `what?q=1` bring their neighbors' bytes, and the row fails there too.
  Of the native origins, only `posixv2`, whose storage is on its own
  host, answers an HTML listing at all. An origin that answers none is
  passed over, and named in the note, since `collection-get` fails it;
  where none answers one, the row skips, as it does where the origins
  take no direct clients.
- `plain-object-listing`: a PROPFIND of Depth 1 of an object, which is
  no collection, must answer as Depth 0 would (RFC 4918, section 9.1):
  207, listing the object alone, with its size. Every origin and
  federated cache is asked so of `tree/b` in `/protected-a`, and must
  answer so, or refuse where it may not list. Then `pelican object ls -l
  --json` of it must list the object alone, where `/protected-a` has
  `Listings` (Pelican's `TestListPlainObjectPosixV2`).
- XRootD falls short of some of this (XRootD 5.9.7, in the v26.0.0-rc.3
  images), so where it serves the origins or the caches
  (`-p origin-xrootd`, `-p cache-xrootd`), these rows fail. An XRootD
  origin answers Depth infinity as Depth 1 would, listing the first
  level alone (`origin-infinity`, and `cache-infinity` through it), and
  its HTML listing comes with no `Content-Type` (`collection-get`). An
  XRootD cache lists no collection: a PROPFIND of one deeper than Depth
  0 is refused, with 409 (`is a directory`) or 502, whatever the token
  (`cache-depth-1`, `cache-infinity`, `cache-body`, `public-no-token`,
  and `new-object`'s cache rows); Pelican's own client never asks a
  cache for a listing. Depth 0 of an object, and Depth 1 of one
  (`plain-object-listing`), it answers.

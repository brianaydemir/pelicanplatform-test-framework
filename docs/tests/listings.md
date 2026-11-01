# Listings

Part of [What the tests check](README.md).

The [`listings`](../../framework/suites/listings.py) suite sends PROPFIND
straight to every origin, director, and federated cache, at depths 0, 1, and
infinity, in every [namespace](../glossary.md#namespace). Depth 0 is a stat,
allowed wherever the namespace may be read; deeper is a listing, allowed
only with `Listings`. An origin answers only direct clients (`DirectReads`).
A PROPFIND that may not be answered must get 401, 403, or 405. Every object
listed must have a `getcontentlength`.

The suite [skips](../glossary.md#skip) where the shape does not export
`/protected-a`; its `cache-*` scenarios skip without a federated cache.
`protected-a-no-token` and `public-no-token` run in the WLCG
[pass](../glossary.md#pass) alone
([token profiles](README.md#token-profiles)).

- `origin-depth-0`, `origin-depth-1`: Each origin must list the tree
  exactly, with each object's `getlastmodified` within a minute of its
  writing, in the right time zone (`TZ_STORAGE`;
  [Configuration](../configuration.md#knobs)).
- `origin-infinity`: Each origin must list the whole tree, or refuse with
  403 ([RFC 4918](https://www.rfc-editor.org/rfc/rfc4918)), but not answer
  as Depth 1.
- `director`: Each director must redirect to an origin, which must answer as
  in `origin-depth-0` and `origin-depth-1`. Skips for a standalone origin
  ([`-p topo-standalone`](../../framework/presets/topo-standalone.sh)).
- `cache-depth-0`: Each cache must answer Depth 0 of an object, before and
  after it holds it.
- `cache-depth-1`: Each cache must answer Depth 0 of a collection, and list
  it at Depth 1 as the origins do.
- `cache-infinity`: The same at Depth infinity. A cache may refuse with 403
  only if an origin does, or none may be asked directly.
- `cache-body`: The same at Depth 1, with the body that
  [`pelican object ls`](../glossary.md#pelican-client) sends.
- `protected-a-no-token`, `protected-a-scope`: Every origin and cache must
  refuse to list `/protected-a` with no token, or one scoped elsewhere.
- `public-no-token`: Every origin and cache must list `/public` with no
  token. Skips without `/public`.
- `new-object`: An object must be listed at once, by PROPFIND and
  `pelican object ls`, and be gone once removed. Needs `Listings`.
- `collection-get`: A GET of a collection must bring no object's bytes. An
  origin must answer `Accept: text/html` with an HTML listing. A cache must
  not keep the answer as an object; a
  [V2 cache](../glossary.md#native-and-xrootd)'s 200 must be
  `application/octet-stream`, with `X-Content-Type-Options: nosniff` and no
  `Age`.
- `html-links`: Each link in an origin's HTML listing of the
  [`names`](names.md) suite's objects, followed as a browser would, must
  lead to its entry, never a decoy or a 404. Every entry must have a link,
  and a native origin's listing must carry `Cache-Control: no-store`. Skips
  where no origin answers an HTML listing, or the origins take no direct
  clients.
- `plain-object-listing`: Depth 1 of an object must answer as Depth 0
  ([RFC 4918, section 9.1](https://www.rfc-editor.org/rfc/rfc4918#section-9.1)):
  207, the object alone, with its size. So must
  `pelican object ls -l --json`, given `Listings`.

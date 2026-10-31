# Names

Part of [What the tests check](README.md).

The `names` suite puts objects in `/protected-a` whose names a URL or a
listing must encode: a space, `%`, `+`, `#`, `?`, `&`, `;`, quotes,
angle brackets, brackets, `*`, and letters beyond ASCII. The tests
percent-encode every byte of a name but RFC 3986's unreserved characters
(letters, digits, and `-._~`), in URLs and in what they give a client.
Two more names are easily mishandled: a leading dot, and 200
characters. Each sits beside decoys that a mishandled encoding would
fetch instead: `x%41y` beside `xAy`, `a+b` and `a%20b` beside `a b`,
`hash#tag` beside `hash`, and `what?q=1` beside `what`. Any bytes but
the object's own fail its row.
A row's note names the first failure; the log lists them all. The suite
skips where `/protected-a` is not exported.

- `origin-put`: A PUT of each name must land in each store under exactly
  that name, and nothing else may; in a pstore origin's catalog, with
  the object's size and checksums. Skips where `/protected-a` takes no
  writes.
- `origin-get`, `cache-get`: Every origin, and every cache, cold, must
  serve each object by its name. `origin-get` skips where `/protected-a`
  takes no direct clients.
- `director-get`: Every director's object route must redirect a plain
  HTTP client to a federated cache, or with none, to an origin, with a
  `Location` that, followed as given, gets the object. Skips at a
  standalone origin.
- `origin-list`, `cache-list`: Every origin and federated cache must
  list each name once, decoded, with its size. Each skips where
  `/protected-a` cannot be [listed](listings.md) there.
- `client-get`: `pelican object get` of each object must write a file of
  its name, with its bytes, and nothing else: through the director's
  cache; direct (`--direct`), where `/protected-a` takes direct clients;
  and from each federated cache, named (`--cache`).
- `client-put`: `pelican object put` of a file of each name must land as
  the object of that name, and nothing else. Skips where `/protected-a`
  takes no writes.
- `plugin-get`, `plugin-put`: The same through `stash_plugin`. A named
  cache must be the result ad's endpoint.
- `client-ls`: `pelican object ls` must list each name once, decoded,
  with its size.

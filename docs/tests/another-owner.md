# Another owner

Part of [What the tests check](README.md).

Under `-p topo-multi-owner`, origin-2 has a key and an issuer of its
own, and exports `/other`, like `/protected-a`, and `/public/other`,
like `/public` and nested in it (as far as `ORIGIN_NAMESPACES` exports
those). Where `/public` is exported, origin-2 also exports
`/public/secret`, nested in it too, which takes a token like
`/protected-a` but mirrors no namespace: the `auth` suite's
`nested-protected` check asks for it ([authorization](authorization.md)),
and the `owners` suite pairs it with nothing. `fed.sh` describes origin-2
in `framework/var/generated/owners` and `owner-exports`; the other
suites test only the origins' namespaces. The `owners` suite checks:

- `ready`: origin-2's objects come back through the federation and from
  each cache, with 20 tries, 15 seconds apart. It runs first, whatever
  else is asked for; if origin-2 never serves, the suite stops.
- `keys`: origin-2's server-wide JWKS, and its issuer's for `/other`,
  hold its keys, and none of the origins' or the test keys; the origins'
  hold none of its.
- `routing`: each director sends a client's direct read (`?directread`)
  of origin-2's prefixes to it alone, and of the origins' to them alone;
  where an export takes no direct clients, the director must find no
  origin (405) and name none, not even the origins', whose `/public`
  encloses `/public/other`. Through each cache,
  `/public/other/...` must be origin-2's object, never a decoy that
  origin-0's `/public` holds at `other/...`, and
  `/public/others-<run>/...` origin-0's, since a prefix matches whole
  path segments.
- One scenario per credential of `framework/testlib/owners.py`, on both
  sides of each pair (`/other` and `/protected-a`; `/public/other` and
  `/public`), with GET, HEAD, PUT, and DELETE straight to the side's
  origins, and GET and HEAD to each cache:

  | Credential    | Token                                                          |
  | ------------- | -------------------------------------------------------------- |
  | `owner`       | origin-2's key and issuer                                      |
  | `origins`     | the origins' key and issuer                                    |
  | `owner-key`   | origin-2's key, naming the origins' issuer                     |
  | `origins-key` | the origins' key, naming origin-2's issuer                     |
  | `jwks`        | `test-keys/jwks.pem`, naming origin-2's issuer                 |
  | `ns-jwks`     | `test-keys/ns-protected-a.pem`, naming origin-2's issuer       |
  | `none`        | none                                                           |

  Where a token decides, only `owner` on origin-2's side and `origins`
  on the origins' should be allowed; the capabilities then apply as in
  [authorization](authorization.md). The `/public` pair gets only `owner`,
  `origins`, and `none`, unless `/public` takes writes
  (`-p origin-public-writes`), when each side has an issuer that decides
  a write there, and it gets every credential but `ns-jwks`, since no
  public namespace has keys of its own.
- `get`, `direct-get`, `put`, `cross-get`, and `cross-put`: the client
  moves origin-2's objects through a cache (served by one) and from
  origin-2 (served by it), uploads with origin-2's token to each of its
  exports that takes writes (`/other`, and `/public/other` under
  `-p origin-public-writes`), and presents each owner's token in the
  other's namespace, which must be refused with nothing stored.

# Test data

Part of [What the tests check](README.md).

Each [namespace](../glossary.md#namespace) has storage of its own at each
origin, under `framework/var/data/origin/<N>/<namespace>/`.
[`./fed.sh init`](../../fed.sh) writes 16 objects, `<origin>.<n>`, to each
namespace's `data/`. Each holds its own path and a newline (e.g.
`/protected-a/data/0.3`), so a server that answers from another namespace's
storage is caught.

The [`transfers`](../../framework/suites/transfers.py) suite moves these
objects through each [client](../glossary.md#client), in batches of one
client invocation each. `<ns>` is each namespace, and `<cred>` each
[credential](authorization.md) that applies to it:

| Scenario          | Transfers                                                                                                                                    |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `exist`           | get `/public/data/0.<n>`, with no token                                                                                                      |
| `dne`             | get `/public/data/9.<n>`, which does not exist; where `/public` is not exported, from the first protected namespace, with its `server` token |
| `get-<ns>-<cred>` | get `/<ns>/data/0.<n>`, presenting `<cred>` (`get-public-none` is `exist`)                                                                   |
| `put-<ns>-<cred>` | put `/<ns>/data/put/put-<ns>-<cred>/<x>.<z>`, presenting `<cred>`                                                                            |

These run through the [Pelican client](../glossary.md#pelican-client)'s
`pelican object get` and `put`. Each get has two
[route](../glossary.md#route) [twins](../glossary.md#twin):
`direct-<scenario>` gets from an origin (`--direct`), and `cache-<scenario>`
from the first federated cache, named (`--cache`). Every scenario also has a
[`stash_plugin`](../glossary.md#stash_plugin) twin, `plugin-<scenario>`
(e.g. `plugin-direct-exist`), and an HTTP twin, `http-<scenario>`: a
[plain HTTP client](../glossary.md#plain-http-client) that asks the
director, or the named cache, and follows redirects as `curl -L` does, to
the `Location` as given, without the `Authorization` header. So a director's
redirect must carry the token in `authz`.

Without a federated cache
([`-p topo-standalone`](../../framework/presets/topo-standalone.sh),
[`-p topo-no-cache`](../../framework/presets/topo-no-cache.sh)), there are
no `cache-` scenarios, and gets meant for a cache must go to an origin. At a
standalone origin, the `http-` scenarios [skip](../glossary.md#skip).
Scenarios that present no token (`exist` and those of `none`) run in the
WLCG [pass](../glossary.md#pass) only
([token profiles](README.md#token-profiles)). Before its gets, the suite
uploads a [pstore](pstore.md) origin's objects; in that store, `/public`
reads `/protected-a`'s storage.

## Expected outcomes

Each scenario should fare as the [authorization](authorization.md) rules
say. `./fed.sh test -l transfers` lists the scenarios, and the summary shows
what each should do. A shape that exports one namespace has only its
scenarios, and `dne` gets from it. Under
[`-p origin-no-direct`](../../framework/presets/origin-no-direct.sh), every
direct read, `direct-dne` included, must be refused. The director refuses
any expired token it sees, so `http-get-public-expired` and
`http-direct-get-public-expired` should be refused, and
`http-cache-get-public-expired`, which asks the cache, should pass.

`exist`, `dne`, and each scenario that should pass run two batches, of one
object and then two (a put of two goes to a collection); the rest run one,
of one object.

## Judging the batches

- Every file downloaded must match the object in the origin's store, and
  every upload what the store received.
- Every batch that should be refused must fail by refusal, moving nothing. A
  server error (5xx), timeout, refused connection, not found (error 5011),
  transfer error (6xxx), or local `permission denied` is not a refusal, even
  beside a 401 or 403.
- Every `dne` batch must fail with not found (error 5011).
- No batch may leave anything else in its directory, nor a failed get a file
  of the object's name.
- A batch killed after 600 seconds fails as `other`.

The plain HTTP client's failures are its statuses: from the director, 401 is
`director`, 405 `unsupported`, and 404 `not found`; from the server the
redirect led to, 401 or 403 is `server`, and 404 `not found`. Anything else
is `other`, and unexpected.

The summary counts each scenario's batches that passed, failed, and went
unexpectedly. Under `framework/var/data/transfers/`
([where things live](../../README.md#where-things-live)), `verify/problems`
lists each unexpected result, and `log/<scenario>/<batch>` keeps the plain
HTTP client's requests and answers. `read-back` fails where a pstore
origin's uploads cannot be read back.

The `served-by-<server>-<client>` rows check who served the successful
downloads. Only a cache may serve one meant for a cache (an origin, where
there is none); only an origin, a direct one; and only the named cache, a
named one. Under
[`-p director-default-origin`](../../framework/presets/director-default-origin.sh),
an origin must serve the plain HTTP client's bare paths. Under
[`-p topo-tiny`](../../framework/presets/topo-tiny.sh), these rows skip.

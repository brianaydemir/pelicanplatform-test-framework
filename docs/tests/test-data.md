# Test data

Part of [What the tests check](README.md).

Each namespace has storage of its own: a directory of each origin's
store, `framework/var/data/origin/<N>/<namespace>/`, which its export's
`StoragePrefix` leads to (`/data/<namespace>` in the origin, or a path
or a bucket of the `webdav` or `s3` backend). `init` writes 16 objects
named `<origin>.<n>` to each namespace's `data/`. An object's bytes are
its path in the federation, e.g. `/protected-a/data/0.3`, so no two are
alike, and a server that answers from another namespace's storage is
caught. The suites likewise give each namespace's objects bytes of their
own.

The `transfers` suite makes its batches each time it runs, in
`framework/var/data/transfers/input/<scenario>/<index>`. A batch is one
client invocation. `<ns>` is each namespace, and `<cred>` each of the
eleven [credentials](authorization.md) that applies to it:

| Scenario          | Transfers                                                 |
| ----------------- | --------------------------------------------------------- |
| `exist`           | get `/public/data/0.<n>`, with no token                   |
| `dne`             | get `/public/data/9.<n>` (never created): not found; where `/public` isn't exported, from the first exported protected namespace instead, with its `server` token |
| `get-<ns>-<cred>` | get `/<ns>/data/0.<n>`, presenting `<cred>` (`get-public-none` is `exist`); `get-public-<cred>` checks that a configured, possibly bad, token doesn't break a public read, since the client sends none |
| `put-<ns>-<cred>` | put `/<ns>/data/put/put-<ns>-<cred>/<x>.<z>`, presenting `<cred>` |

These run through `pelican object get` and `put`. Each get also has a
direct twin, `direct-<scenario>`, which asks the director for an origin
instead of a cache (`pelican object get --direct`), and a named twin,
`cache-<scenario>`, which names the first federated cache (`pelican
object get --cache`) and takes the objects from it, asking the director
only what the namespace wants: the three ways a client reaches an
object. Every scenario also has a plugin twin, `plugin-<scenario>`
(e.g. `plugin-direct-exist`), which runs the same batches through
`stash_plugin`, and an HTTP twin, `http-<scenario>`, which runs them as
a plain HTTP client, such as curl, would. The plugin has no flag for a
direct read, so its direct batches add `?directread` to each URL, and
takes the named cache from `Client.PreferredCaches`. The plain HTTP
client asks the director for each object's bare path, which it sends
to a cache (or, under `-p director-default-origin`, to an origin), or
with `?directread`, which it sends to an origin; for a named get, it
asks the cache itself; and it PUTs each upload to its bare path, which
the director sends to an origin. It follows each redirect as `curl -L`
does: to the `Location` as given, whose `authz` query parameter carries
the token, and without the `Authorization` header, which curl (since
7.58.0), Go's HTTP client, and Python's drop when the host changes; so
a director that left the token out of its redirect would fail every
protected read. Only the plugin's and the HTTP client's uploads go
elsewhere, to `put/plugin-put-<ns>-<cred>/` and
`put/http-put-<ns>-<cred>/`. A shape without a federated cache
(`topo-standalone`, `topo-no-cache`) has no named scenarios, and its
other gets, asking for a cache, must be sent to an origin; a standalone
origin has no director, so every HTTP twin skips there.

What each scenario should do follows the
[authorization](authorization.md) rules: every get of `/public` should
pass, and every put to it should be refused, unless
`-p origin-public-writes` gives it writes, when the puts with the
`server` and `jwks` tokens should pass, as in the protected namespaces,
and the rest be refused. A shape that exports one
namespace (`origin-httpsv2`, `origin-no-direct`, `origin-public`) has
only that namespace's scenarios, and `dne`, which gets from that
namespace instead. Without `DirectReads` (`origin-no-direct`),
`direct-dne` must be refused. `./fed.sh test -l transfers` lists the
scenarios, and the summary after a run shows what each was expected to
do.

The Pelican clients send no token for a read of `/public`, so a bad one
cannot break it, and every `get-public-<cred>` should pass. A plain
HTTP client sends whatever it is given, and the director refuses an
expired token it is shown, whatever the namespace
(`validateClientToken` in `director/director.go`), while the servers
ignore a token on a public read: so `http-get-public-expired` and
`http-direct-get-public-expired`, which ask the director, should be
refused, by the director, and `http-cache-get-public-expired`, which
asks the cache itself, should pass.

`exist`, `dne`, and each scenario that a token should let through run
two batches, of one object and then two (a put of two goes to a
collection rather than to an object's URL); the rest run one, of one
object. In the default shape that is 357 scenarios and 447 client
invocations (119 and 149 for each of the three clients), 16 at a time.
Each batch picks distinct objects, uniformly at random.
`framework/testlib/transfers.py` sets these numbers.

The suite mints the tokens and writes new random bytes for each upload.
Before the run, it empties every put collection in every store, a `pstore`
origin's included, and stops if it can't. Afterward, it judges each batch by
the rules in `framework/testlib/transfers.py`. Every file downloaded must
match the object in the origin's store, and every upload what the store
received. Every batch that should be refused must fail by being refused,
with nothing moved: no object's bytes in a file it downloaded, and no upload
in any store. The client downloads into a temporary file beside the
object's, and renames it only once the object is complete, so no batch
may leave anything else in its directory, and a get that fails must
leave no file of the object's name. A failure that also shows a server
error (HTTP 5xx), a
timeout, a refused connection, not found (error 5011), or a transfer error
(6xxx) is not a refusal, even beside a 401 or 403; nor is a local
`permission denied`. A batch killed after 600 seconds fails as `other`.
Every `dne` batch must fail with not found (error 5011). Before a `get`
that refuses collections, Pelican's client asks each object server by
PROPFIND whether the path is a collection, and takes a 404 for no
answer (`objectCached` in `client/handle_http.go`), so with one origin,
to which the director sends a client without checking that it holds the
object, it refuses a missing object with a resolution error (2000, "no
object server could confirm whether ... is a collection") rather than
not found, and the `dne`, `direct-dne`, and `cache-dne` rows fail, on
v26.0.0-rc.3 and main alike (L4). With several origins, the director's
presence check answers 404 first, which the client reports as not found.
`stash_plugin` refuses no collection, so its twins pass, and so do the
HTTP twins, which stat nothing. The plain HTTP client's failures are its
statuses: a 401 from the director (an expired token) is `director`, a
405 from it (no origin allows the request in the namespace, as for a
put to `/public`) `unsupported`, a 404 from it or from a server `not
found`, and a 401 or 403 from the server the redirect led to `server`;
anything else, a redirect that led nowhere included, is `other`, and so
unexpected. A get that got 200 goes in the batch's directory under the
object's name, as `curl -o` would put it; nothing else is written. The
summary counts each scenario's batches that passed, failed, and went
unexpectedly, says why the failed ones failed, and names the batch
sizes that had unexpected results. `verify/problems` lists each
unexpected result; `log/<scenario>/<batch>` keeps the plain HTTP
client's requests and the answers to each, its tokens redacted.

Last, it shows which servers served the successful downloads, by client and
route. In each namespace with downloads meant for a cache, a cache must have
served some, since a director that routed around its caches would otherwise
pass every scenario; only an origin may serve a direct one; and only the
named cache a named one, since a client that fell back to the director's
caches would otherwise pass. The plain HTTP client's bare paths go where
the director sends them, so under `-p director-default-origin` only an
origin may serve those too. Under `topo-tiny`, whose cache and origin are
one host, these checks are skipped. Where a namespace takes no direct
clients (`origin-no-direct`), every direct read must be refused.

A `pstore` origin's store is encrypted, and only the running origin can
write it. So `init` writes a plain copy of the objects beside it, in
`data/origin/<N>/<namespace>/data/`, and the `transfers` suite uploads
whatever the store lacks, to `/protected-a` and `/protected-b`, before
its gets. Each has storage of its own in the store, and `/public`, which
takes no writes, reads `/protected-a`'s: the one place two namespaces
share storage. Uploads to it are read back from the origin instead of
from disk.

# Unix users

Part of [What the tests check](README.md).

The `users` suite checks who owns the files that requests straight to
each POSIX origin leave, and what one user can learn of another's. It
runs under `-p origin-multiuser`, where an origin acts as the Unix user
that a token maps to, and under `-p server-unprivileged`, where every
server runs as `pelican`; there, only `owner` and `primary-group` apply.
Native origins are tested; XRootD origins are the control.

Under multiuser, `fed.sh`'s mapfile maps the tests' subject to `pelican`
(uid 10941), and any other to `xrootd` (uid 10940,
`Origin.ScitokensDefaultUser`). Where the host's bind mounts hide
owners, rows that depend on them are inconclusive.

- `owner`: An upload with the tests' token must be `pelican`'s.

- `default-user`: Under multiuser, one with another subject's token must
  be `xrootd`'s.

- `primary-group`: One with the tests' token, which names no group, must
  be in `pelican`'s group (gid 10941).

- `public-tokenless`: Under multiuser, with no token, at each origin, a
  GET of an object in `/public` must serve it (`get`), and a Depth 1
  PROPFIND of its collection must answer 207 and name it (`propfind`),
  as `nobody` (`Origin.ScitokensUnauthenticatedUser`). Skips where
  `/public` isn't exported.

- `bypass`: Under multiuser, at each origin, with `pelican`'s file
  (mode 0600) in its directory (mode 0700), `xrootd` must be refused
  each of these (403 or 404), with no `ETag`, `Last-Modified`, or
  `Digest`, and none of the file's bytes:

  - `get`: a GET of the file;
  - `html-listing`: a GET of the directory with `Accept: text/html`,
    which must not name the file;
  - `if-none-match`: a GET with `If-None-Match: *`, which must not get
    304;
  - `digest`: a HEAD with `Want-Digest: crc32c`;
  - `staging`: under POSC, the last three, of `pelican`'s staging
    directory (`.pelican-posc/pelican/`) and a file in it.

- `posc-multiuser`: Under POSC and multiuser, at each origin:

  - `first`: an upload with the tests' token must succeed, and it and
    its staging directory (`.pelican-posc/pelican/`, mode 0700) be
    `pelican`'s;
  - `second`: then one with another subject's token must succeed, and it
    and `.pelican-posc/xrootd/` (mode 0700) be `xrootd`'s;
  - `read-only-dir`: one with the tests' token into `pelican`'s
    directory of mode 0500 must be refused (403), and land nothing.

- `min-id`: Under multiuser, at each origin, uploads with tokens mapped
  to `root` (`root`) and `daemon` (`daemon`), below
  `Origin.MultiuserMinID` (1000 by default), must be refused (403),
  leaving no file or staging directory.

- `mapfile`: Under multiuser, at each origin, the mapfile's rules:

  - `path`: a rule for `/data/users/mapped` alone, relative to the
    namespace as in XrdSciTokens' mapfile, must map uploads there to
    `pelican`, and no others;
  - `group`: a token whose `wlcg.groups` is `[xrootd]`, which a rule
    maps to `pelican`, must upload as `pelican`, in either profile
    (`Origin.ScitokensGroupsClaim`);
  - `edited`: a rule added to the mapfile while the origins run must
    take effect within 150 seconds (`Origin.UserMapfileRefreshInterval`,
    1m by default); it is removed afterward;
  - `username-claim`: a token whose username claim names `pelican` must
    upload as `pelican`, since the claim overrides
    `Origin.ScitokensDefaultUser`;
  - `unmatched`: a token whose subject matches no rule, with no username
    claim, must be refused (401 or 403), and write nothing.

  For the last two, the suite restarts every origin with
  `Origin.ScitokensUsernameClaim` set and `Origin.ScitokensDefaultUser`
  unset ([service control](../../README.md#service-control)), then
  restores them, waiting up to 150 seconds for the federation to serve
  again. They skip without a control helper, and under `-p topo-tiny`.

# Unix users

Part of [What the tests check](README.md).

Under `-p origin-multiuser`, a POSIX origin (`posixv2`, or XRootD's
`posix` through its multiuser plugin) does every filesystem operation
as the Unix user that the request's token maps to; under
`-p server-unprivileged`, every server drops to the `pelican` user. The
`users` suite sends requests straight to each origin, and checks who
owns the files they leave, and what a user can learn of another's. Its
objects go under `/protected-a/data/users/<run>/`, which it removes
afterward. A scenario with several cases records a row per case, named
`<scenario> <case>`.

Under multiuser, `fed.sh` maps tokens to users with a mapfile
(`framework/var/generated/multiuser-mapfile.json`): the tests' subject
to `pelican` (uid 10941), and any other to `xrootd` (uid 10940),
`Origin.ScitokensDefaultUser`. Its other rules are the suite's, each
for a subject or a group that only the suite's tokens carry: two map to
`root` and `daemon`, and the rest to `pelican`, so that an upload's
owner shows that the rule matched, and not the default. A native origin
is the one tested; an XRootD origin is the control.

Whether a bind mount shows a file's owner depends on the host, so the
suite first gives a file of its own to uid 10941 and gid 10941; where
the store doesn't show that, the rows that depend on owners are
inconclusive.

- `owner`: an upload with the tests' token is the `pelican` user's,
  which `fed.sh` maps the tests' subject to under multiuser.
- `default-user`: under multiuser, an upload with a token for another
  subject is the `xrootd` user's. Under POSC, one of these two users'
  uploads fails, as `posc-multiuser` says.
- `primary-group`: an upload with the tests' token, which names no
  group, must be in the `pelican` user's own group (gid 10941). A
  native multiuser origin acts as the token's first group
  (`wlcg.groups`), and without one, as gid 0, root's
  (`resolveIdentity` in `origin_serve/multiuser_fs.go`), so the row
  fails there, on main and v26.0.0-rc.3 alike.
- `public-tokenless`: under multiuser, with no token, at each origin, a
  GET of an object in `/public` must serve its bytes (`get`), and a
  PROPFIND of Depth 1 of its collection must answer 207 and name it
  (`propfind`). Pelican's documentation says such requests run as
  `nobody` (`Origin.ScitokensUnauthenticatedUser`), but a native origin
  attaches a user only from a token (`authorizeWithContext` in
  `origin_serve/authz.go`), and its filesystem refuses a request with
  none (`resolveIdentity`). So both rows fail there, with 404 and 405,
  on main and v26.0.0-rc.3 alike. They skip where `/public` isn't
  exported.
- `bypass`: under multiuser, at each origin, the suite makes a file of
  the `pelican` user's, mode 0600, in a directory of its own, mode 0700,
  straight in the store. The `pelican` user's own GET of it must serve
  it, or the rows are inconclusive. Then the `xrootd` user (a token for
  another subject) must be refused each of these (403, or 404 from a
  permission error), and get no `ETag`, `Last-Modified`, or `Digest`,
  nor the file's bytes:
  - `get`: a plain GET of the file;
  - `html-listing`: a GET of the directory with `Accept: text/html`,
    which also must not name the file;
  - `if-none-match`: a GET of the file with `If-None-Match: *`, which
    must not get 304;
  - `digest`: a HEAD of the file with `Want-Digest: crc32c`;
  - `staging`: under POSC, the same three, of POSC's staging directory
    for the `pelican` user (`<storage>/.pelican-posc/pelican/`, mode
    0700), and of a file in it, mode 0600, that the suite makes there.

  A native origin answers these paths from the store itself, as its own
  process, before it asks the filesystem that acts as the user
  (`handleGetWithETag`, `streamDirectoryListing`, and
  `handleHeadWithChecksum` in `origin_serve/handlers.go`): a plain GET
  gets 404 with the file's `ETag` and `Last-Modified`; the listing is
  served; `If-None-Match: *` gets 304; and the HEAD gets 404 with a
  `Digest`. So every row fails there, on main and v26.0.0-rc.3 alike.
- `posc-multiuser`: under POSC and multiuser, at each origin:
  - `first`: an upload with the tests' token must succeed, and be the
    `pelican` user's, as must its staging directory,
    `<storage>/.pelican-posc/pelican/`, mode 0700.
  - `second`: then an upload with another subject's token must succeed,
    and be the `xrootd` user's, as must `.pelican-posc/xrootd/`, mode
    0700.
  - `read-only-dir`: an upload with the tests' token into a directory of
    the `pelican` user's, mode 0500, must be refused (403), and land
    nothing.

  POSC sits beneath the multiuser filesystem, so the first upload into
  a store, whoever's, makes the staging directory, and `.pelican-posc`
  above it, as its own user, both mode 0700 (`ensureUserDir` in
  `origin_serve/posc.go`), and no other user can make its own there. In
  a full run, the `posc` suite's uploads, with the tests' token, come
  first, so `first` passes, and `second` fails, as does `default-user`.
  Run alone, into a store with no `.pelican-posc` yet, `default-user`
  comes first, and `first` fails instead. A failing row's note says who
  owns `.pelican-posc`. The rename into place runs as the origin's own
  process (`poscFile.Close`), so `read-only-dir` fails too. It uploads
  as the `pelican` user, who can stage, since an upload of the `xrootd`
  user's would be refused for the wrong reason. Both fail on main and
  v26.0.0-rc.3 alike.
- `min-id`: under multiuser, at each origin, an upload with a token
  that the mapfile maps to `root` (`root`), and one to `daemon`
  (`daemon`), whose uids are below `Origin.MultiuserMinID` (1000 by
  default), must be refused (403), and leave no file, nor, under POSC,
  a staging directory for the user. A native origin refuses the user
  (`identity.WithMinID`), but with 404, since the error isn't a
  permission error, so the rows fail there on their status alone, on
  main and v26.0.0-rc.3 alike.
- `mapfile`: under multiuser, at each origin, uploads whose owners show
  the mapfile's rules at work:
  - `path`: with a token for a subject that the mapfile maps to
    `pelican` under `/data/users/mapped` alone, an upload under
    `/protected-a/data/users/mapped/` must be the `pelican` user's, and
    one elsewhere must not. In XrdSciTokens' mapfile, which
    Pelican's documentation cites, a `path` is relative to the
    namespace; the mapfile also has the rule with the federation's
    path, `/protected-a/data/users/mapped`, so that either reading
    passes. A native origin maps a token to a user with no path at all
    (`UserMapper.MapTokenToUser` in `origin_serve/user_mapfile.go`), so
    no `path` rule matches, and the row fails there, on main and
    v26.0.0-rc.3 alike.
  - `group`: an upload with a token whose `wlcg.groups` is `[xrootd]`
    (in the SciTokens pass too, since that is the claim the origins
    read, `Origin.ScitokensGroupsClaim`'s default), which the mapfile
    maps to `pelican`, must be the `pelican` user's.
    The group is a Unix group's name, since a native origin acts as the
    token's first group, and refuses a request whose group it can't
    find.
  - `edited`: an upload with a token for a subject of the run's must
    not be the `pelican` user's. Then the suite adds a rule to the
    mapfile, in place, mapping the subject to `pelican`, and an upload
    with a new token for it must be the `pelican` user's within 150
    seconds. Pelican rereads the mapfile every
    `Origin.UserMapfileRefreshInterval` (1m by default), and XRootD its
    configuration every 60 seconds. Both map a token to a user once,
    and keep it, so each try has a new token. The suite restores the
    mapfile afterward. This first upload, and `path`'s elsewhere, which
    no rule maps, may be the `xrootd` user's or be refused, since under
    POSC the `xrootd` user may be unable to stage (`posc-multiuser`).
  - `username-claim` and `unmatched` need configuration that no shape
    has, so the suite restarts every origin with
    `Origin.ScitokensUsernameClaim` set and
    `Origin.ScitokensDefaultUser` unset, as the last of its
    configuration layers
    ([service control](../../README.md#service-control)), and restores
    them afterward. Then every origin must serve a probe object, every
    director name every origin for it, and every federated cache serve
    it, within 150 seconds. The restart needs a control helper and a
    topology other than `tiny`; elsewhere, the rows skip, and say why.
    `username-claim`: an upload with a token whose username claim names
    `pelican` must be the `pelican` user's, since Pelican's
    documentation says the claim overrides
    `Origin.ScitokensDefaultUser`. A native origin with a mapfile takes
    the claim only where a rule maps it, and otherwise the default
    user, here none, so it drops the token, and the row fails there, on
    main and v26.0.0-rc.3 alike. `unmatched`: an upload with a token
    whose subject matches no rule, and which has no username claim,
    must be refused (401 or 403), and write nothing. A native origin
    drops the token (`authConfig.loader` in `origin_serve/authz.go`);
    XRootD refuses it for lacking the claim.

Under `-p server-unprivileged`, only `owner` and `primary-group` apply;
the rest skip.

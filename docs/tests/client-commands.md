# Client commands

Part of [What the tests check](README.md).

The `commands` suite tests the client's commands beyond the `transfers`
suite's: `ls`, `stat`, `delete`, `copy`, `sync`, and `du`, in `/public`
and `/protected-a` where they apply; and `get` and `put` where Pelican's
`docs/object-transfer-semantics.md` says what each must do with what
already exists. Writes to `/public` (`delete-public`, `copy-to-public`)
must be refused (or, where it takes writes, `-p origin-public-writes`,
land), and so must any command that needs a capability its namespace
lacks (`Listings` for `ls`, `du`, `sync`, `get` of a collection,
`delete -r`, and `put` into a collection; `Writes` for `delete`, `put`,
`copy` to the federation, and `sync` to it; `DirectReads` for
`copy --direct`), with nothing changed. `./fed.sh test -l commands`
lists the scenarios.

- The read-only commands work on a small tree that the test puts
  straight at every origin, under `/<ns>/data/cmd/<run>/tree/`, so that
  they agree whichever origin the director picks: the same paths and
  sizes in each namespace, but bytes of its own. Listings
  (`ls-protected-a`, `ls-public`), sizes, checksums (`crc32c` and `md5`,
  in hex, as `pelican object stat` prints them, those the origins'
  storage reports; see [block boundaries](block-boundaries.md)), and
  `du`'s totals must match it, in `/protected-a` and, with no token, in
  `/public`; `stat`'s modification time must be the file's, within a
  minute (a pstore origin's, as its catalog records it, which its
  storage API gives; see [pstore](pstore.md)); `stat` of a collection
  must say it is one (`stat-collection`); and a copy from `/public`
  (`copy-tpc-public`) must hold `/public`'s bytes. `stat` names every
  kind of checksum at once, and the client asks for them in one
  `Want-Digest`, which XRootD's HTTP server answers with one digest, the
  first it supports (as the `blocks` suite's `digest-relay` says of a V2
  cache over XRootD origins), so where XRootD serves the origins or the
  caches (`-p origin-xrootd`, `-p cache-xrootd`) `stat-protected-a` and
  `stat-public` get `crc32c` alone, and fail.
- `delete`, `copy`, and `sync` go through the director like any client.
  Under `topo-multi-origin` their effects land on one origin, so a
  success must show in some store, and a refusal in none. `delete` must
  remove an object (`delete-object`), and with `-r` a collection and
  what it holds (`delete-recursive`); of a non-empty collection without
  `-r` (`delete-nonempty`), or with a read-only token
  (`delete-read-only`), it must be refused. `copy` must store a local
  file in `/protected-a` (`copy-upload`), and bring an object's bytes to
  one (`copy-download`). A copy between two federation URLs is a
  third-party copy, which the destination origin pulls: from
  `/protected-a` (`copy-tpc`) or `/public` (`copy-tpc-public`) to
  `/protected-a`, or, with `--direct`, from an origin
  (`copy-tpc-direct`). Every service here has a private address, which
  origins refuse to pull from by default, so
  `framework/config.d/base/40-origin.yaml` allows it.
- `sync` of a local tree to a new collection (`sync-upload`), of a
  collection to a local directory (`sync-download`), and of a collection
  to another, by third-party copies (`sync-tpc`), must copy every file.
  It compares sizes only and never deletes; the scenarios check what it
  uploads by its `--dry-run` output: nothing, of a tree the origins
  already hold (`sync-unchanged`), and only the local file whose size
  changed (`sync-changed`), which `sync` then uploads.
- These commands' exit status says little about what went wrong (mostly
  1 or 11), so the tests require only a non-zero one, and judge
  failures by what the commands print, as the `transfers` suite judges
  them. `delete` prints a refusal as `Remove <path>: 400`, whatever the
  origin answered (a native origin 401, an XRootD origin 403; the
  WebDAV client it removes with turns the refusal into a 400 of its
  own), so `delete-read-only` fails, on v26.0.0-rc.3 and main alike,
  though nothing is removed. At a standalone origin
  (`-p topo-standalone`), whose answer to the client names no
  collections URL, the client takes the other path, a plain DELETE,
  and on the origin's 403 dies of a nil pointer ("Unrecoverable error
  (panic) in DoDelete"), which `delete-read-only` reports as a failure
  that is no refusal (seen with v26.0.0-rc.3, under `-p topo-standalone -p origin-ssh`); nothing is removed there either.
- `ls -r` must list every object by its path in the tree, once
  (`ls-recursive`).
- With several origins (`-p topo-multi-origin`), the director asks the
  origins which holds a path before it redirects, by a HEAD
  (`Director.CheckOriginPresence`; `queryOrigins` in `director/stat.go`),
  when the request needs no token or carries the namespace's. A native
  origin answers a HEAD of a collection with 405, which the director
  takes for absence, as it does any status but 200, 404, and 403, so
  it finds no origin holding any collection (404, "No sources reported
  possession"), and every command that has it find one fails there:
  `ls-public`, `du-public`, `get-recursive`, `copy-tpc`,
  `copy-tpc-public`, `copy-tpc-direct`, `sync-download`, `sync-tpc`,
  `sync-unchanged`, and `sync-changed`, over native origins, on
  v26.0.0-rc.3 and main alike. `ls-protected-a` and `du-protected-a`
  pass, since `ls` and `du` show the director no token, so it does not
  ask; and with one origin it assumes the path is there
  (`Director.AssumePresenceAtSingleOrigin`).
- `du --json` must give each collection's bytes, objects, and
  collections, and `du --count`, whose counts only its text output
  shows, must print the same (`du-protected-a`, `du-public`); `du -s`,
  only the tree's total (`du-summarize`).
- `ls`, `stat`, and `delete` of a missing path must fail with not found
  (`ls-missing`, `stat-missing`, `delete-missing`).
- `ls`, `delete`, and `du` of `/protected-a` with no token
  (`ls-no-token`, `delete-no-token`, `du-no-token`) must be refused by
  the client itself, for want of a token (error 4010), before it asks
  any server; the `auth` and `listings` suites check the servers. A
  standalone origin has no director to tell the client that the
  namespace takes a token, so there the origin's refusal will do.
- `get` onto a local file larger than the object must leave the object
  alone in it (row G1 of that document), and a failed `get` onto it must
  leave it as it was. The failed `get` is of an object that does not
  exist, and must fail with not found: Pelican's client reports it as a
  resolution error instead (2000, "no object server could confirm
  whether ... is a collection"), with one origin, so `get-existing-file`
  fails there, on v26.0.0-rc.3 and main alike (see
  [test data](test-data.md)). A `get` of a collection without `-r`
  (`get-collection`) must be refused, rather than write the listing into
  a file (G4); `get -r` (`get-recursive`) lays the collection out flat,
  in an existing directory or a new one (G5, G6).
- With overwrites off, as by default, `put` to an existing object
  (`put-exists`) must be refused, and change nothing (P2); with them on,
  a smaller file put over an object (`put-overwrite`) must replace it
  whole. `put` of a file to a collection (`put-into-collection`) lands
  in it by the file's name (P3); of a directory without `-r`
  (`put-directory`), it must be refused, and upload nothing (P4); and
  `put -r` of a tree, with an empty file in it, lays it out flat, under
  a new collection or an existing one (P5, P6). An `ssh` origin answers
  the empty file's PUT with 405 (v26.0.0-rc.3), so `put-recursive` fails
  there.

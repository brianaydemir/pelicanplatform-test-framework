# Client commands

Part of [What the tests check](README.md).

The `commands` suite tests the client's commands beyond the `transfers`
suite's: `ls`, `stat`, `delete`, `copy`, `sync`, and `du`, in `/public`
and `/protected-a` where they apply; and `get` and `put` where Pelican's
`docs/object-transfer-semantics.md` says what each must do with what
already exists. Writes to `/public` must be refused, and so must any
command that needs a capability its namespace lacks (`Listings` for
`ls`, `du`, `sync`, and recursive `get`; `Writes` for `delete`, `put`,
`copy` to the federation, and `sync` to it; `DirectReads` for `copy
--direct`), with nothing changed. `./fed.sh test -l commands` lists the
scenarios.

- The read-only commands work on a small tree that the test puts
  straight in every origin's store, under `/<ns>/data/cmd/<run>/tree/`,
  so that they agree whichever origin the director picks: the same paths
  and sizes in each namespace, but bytes of its own. Listings, sizes,
  checksums (`crc32c` and `md5`, in hex, as `pelican object stat` prints
  them, those the origins' storage reports; see
  [block boundaries](block-boundaries.md)), and `du`'s totals must match
  it, `stat`'s modification time must be the file's, within a minute,
  and a copy from `/public` must hold `/public`'s bytes.
- `delete`, `copy`, and `sync` go through the director like any client.
  Under `topo-multi-origin` their effects land on one origin, so a success
  must show in some store, and a refusal in none. A copy between two
  federation URLs is a third-party copy, which the destination origin pulls.
  Every service here has a private address, which origins refuse to pull
  from by default, so `framework/config.d/base/40-origin.yaml` allows it.
- `sync` compares sizes only and never deletes; the scenarios check what
  it uploads by its `--dry-run` output.
- These commands' exit status says little about what went wrong (mostly
  1 or 11), so the tests require only a non-zero one, and judge
  failures by what the commands print, as the `transfers` suite judges
  them.
- `ls -r` must list every object by its path in the tree, once.
- `du --json` must give each collection's bytes, objects, and
  collections, and `du --count`, whose counts only its text output
  shows, must print the same.
- `ls`, `delete`, and `du` of `/protected-a` with no token must be
  refused by the client itself, for want of a token (error 4010),
  before it asks any server; the `auth` and `listings` suites check the
  servers.
- `get` onto a local file larger than the object must leave the object
  alone in it (row G1 of that document), and a failed `get` onto it must
  leave it as it was. A `get` of a collection without `-r` must be
  refused, rather than write the listing into a file (G4); `get -r` lays
  the collection out flat, in an existing directory or a new one (G5,
  G6).
- With overwrites off, as by default, `put` to an existing object must be
  refused, and change nothing (P2); with them on, a smaller file put
  over an object must replace it whole. `put` of a file to a collection
  lands in it by the file's name (P3); of a directory without `-r`, it
  must be refused, and upload nothing (P4); and `put -r` of a tree, with
  an empty file in it, lays it out flat, under a new collection or an
  existing one (P5, P6).

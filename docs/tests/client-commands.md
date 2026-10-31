# Client commands

Part of [What the tests check](README.md).

The `commands` suite tests the
[Pelican client](../glossary.md#pelican-client)'s `ls`, `stat`,
`delete`, `copy`, `sync`, and `du`; and `get` and `put` against what
already exists, citing the rows (G1, P4, ...) of Pelican's
`docs/object-transfer-semantics.md`. It skips where `/protected-a` is
not exported, and its `*-public` scenarios where `/public` is not.
Scenarios that present no token run in the WLCG pass only
([token profiles](README.md#token-profiles)).

A command must be refused, and change nothing, where its namespace lacks
the `Listings`, `Writes`, or `DirectReads` it needs. Writes to `/public`
must be refused, unless `-p origin-public-writes` gives it writes.
Failures are judged by what the command prints, not by its exit status.

- `ls`: `ls -l --json` must list names, sizes, and collections
  (`ls-protected-a`, `ls-public`). `ls -r` must list every object once,
  by its path (`ls-recursive`).
- `stat`: `stat --json --checksums` must report the size, the
  modification time (within a minute), and the storage's `crc32c` and
  `md5` (`stat-protected-a`, `stat-public`); of a collection, that it is
  one (`stat-collection`).
- `delete`: Must remove an object (`delete-object`), and with `-r` a
  collection (`delete-recursive`). Must refuse, removing nothing, a
  non-empty collection without `-r` (`delete-nonempty`), a read-only
  token (`delete-read-only`), and `/public` (`delete-public`). Skips
  where the storage cannot delete.
- `copy`: Must upload a local file (`copy-upload`) and download an
  object (`copy-download`). Third-party copies into `/protected-a` must
  land: from `/protected-a` (`copy-tpc`), from `/public`
  (`copy-tpc-public`), and with `--direct`, from an origin
  (`copy-tpc-direct`). A copy to `/public` must be refused
  (`copy-to-public`).
- `sync`: Must copy a local tree to a new collection (`sync-upload`), a
  collection to a local directory (`sync-download`), and a collection to
  another (`sync-tpc`). `--dry-run` must list nothing for an unchanged
  tree (`sync-unchanged`), and only a file whose size changed
  (`sync-changed`).
- `du`: `du --json` and `du --count` must give each collection's bytes,
  objects, and collections (`du-protected-a`, `du-public`); `du -s`,
  only the total (`du-summarize`).
- Missing paths: `ls`, `stat`, and `delete` must fail with not found
  (`ls-missing`, `stat-missing`, `delete-missing`).
- No token: `ls`, `delete`, and `du` of `/protected-a` must be refused
  by the client (error 4010) before it asks any server (`ls-no-token`,
  `delete-no-token`, `du-no-token`); at a standalone origin, by the
  origin.
- `get`: Onto a larger local file, must leave only the object (G1), and
  a failed one must leave the file as it was (`get-existing-file`). Of a
  collection without `-r`, must be refused (G4; `get-collection`).
  `get -r` must lay a collection out flat, in an existing or a new
  directory (G5, G6; `get-recursive`).
- `put`: To an existing object, must be refused with overwrites off, the
  default (P2; `put-exists`), and replace it whole with them on
  (`put-overwrite`). To a collection, must land by the file's name
  (P3-cli; `put-into-collection`). Of a directory without `-r`, must be
  refused (P4; `put-directory`). `put -r` of a tree with an empty file
  must lay it out flat, under a new or an existing collection (P5, P6;
  `put-recursive`).

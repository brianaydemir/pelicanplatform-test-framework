# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Back the origin with the Pelican store (encrypted blocks plus an
# embedded catalog).
#
# Nothing outside the running origin can write the store, so init-data.py
# writes a plain copy of the test objects beside it, which the transfers
# suite uploads to /protected-a and /protected-b before its transfers.
# Each has storage of its own in the store, and /public, which takes no
# writes, reads /protected-a's.
#
# The memory limit leaves room for two backups of the catalog, as the
# pstore suite schedules them: each takes about half a gigabyte, whatever
# the catalog's size (BadgerDB's Backup streams it through eight
# goroutines, each with buffers of 32 MiB), and the one before may not
# yet have been given back.

ORIGIN_VARIANT=pstore
ORIGIN_MEM=1536M

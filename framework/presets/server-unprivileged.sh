# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Set Server.DropPrivileges on every Pelican server: each starts as root,
# as the images run it, and then becomes the `pelican` user (uid 10941 in
# the images). fed.sh does only what an admin would for that user: it
# lets any uid write the origins' and caches' stores, and read the TLS
# key. Whatever else a server needs, Pelican must arrange for itself, so
# this shape shows where it does not.
#
# From reading Pelican v26.0.0-rc.0 (not yet from a run), expect these to
# fail:
#
#   - Every server chmods and chowns Server.UIPasswordFile, which here is
#     a read-only Compose secret (config/config.go).
#   - The V2 cache makes its persistent-cache and db directories as root,
#     before it drops, and then cannot write them (local_cache/).
#
# On a Linux host, Pelican gives each server's issuer keys and database
# to the pelican user, which can leave you unable to read them. `./fed.sh
# keys add` and `rotate` then refuse, until ./reset.sh.
#
# fed.sh refuses `origin-ssh`, whose private key only you can read, and
# `origin-multiuser`, which needs root.

SERVER_DROP_PRIVILEGES=true

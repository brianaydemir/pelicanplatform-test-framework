# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Set Server.DropPrivileges on every Pelican server: each starts as root,
# as the images run it, and then becomes the `pelican` user (uid 10941 in
# the images). fed.sh does only what an admin would for that user: it
# lets any uid write the origins' and caches' stores, and read the TLS
# key. Whatever else a server needs, Pelican arranges for itself.
#
# On a Linux host, Pelican gives each server's issuer keys and database
# to the pelican user, which can leave you unable to read them. `./fed.sh
# keys add` and `rotate` then refuse, until ./reset.sh, so smoke.sh never
# rotates keys in this shape. The users suite checks who owns what the
# origins write.
#
# fed.sh refuses `origin-ssh`, whose private key only you can read, and
# `origin-multiuser`, which needs root.

SERVER_DROP_PRIVILEGES=true

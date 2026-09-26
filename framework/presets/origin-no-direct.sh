# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Set Origin.DisableDirectClients: the origins serve only caches, which
# prove it with a federation token from the director. A direct request
# gets 401, and the director finds no origin for a direct read (405).
#
# Pelican then refuses exports that take writes, list, allow direct
# reads, or mix public reads with token-protected ones, so the origins
# export only /protected-a, and only for reading. The tests put their
# objects straight in the origins' stores, and expect every direct
# request and every write to be refused. fed.sh refuses `origin-pstore`,
# which only its writes can fill.

ORIGIN_DISABLE_DIRECT_CLIENTS=true
ORIGIN_NAMESPACES=protected-a

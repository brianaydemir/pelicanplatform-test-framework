# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Have the directors send a GET of a bare path, e.g.
# https://director-0:8444/public/data/0.0, to an origin rather than to a
# cache (Director.DefaultResponse), as a federation without caches would.
# Only clients that name no route ask so: curl and browsers, not the
# Pelican client, which asks for a cache or an origin explicitly. The
# federation suite's `redirects` scenario follows such redirects, and
# checks where they lead. fed.sh refuses `topo-standalone`, which has no
# director.

DIRECTOR_DEFAULT_RESPONSE=origin

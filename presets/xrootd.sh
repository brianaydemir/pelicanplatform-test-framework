# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Put XRootD in front of the origin and the cache, as the OSDF runs them,
# and enable the origin's "log in with CILogon" button.
#
# etc/oidc-client-{id,secret} are placeholders, so that button does not
# work unless you register a client at https://cilogon.org/oauth2/register
# and replace both files. Nothing else depends on it.

ORIGIN_VARIANT=posix
CACHE_VARIANT=xrootd

ORIGIN_ENABLE_OIDC=true

# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Put XRootD in front of an HTTPS server (libXrdHTTPServer): the `webdav`
# service, as for `origin-httpsv2`, with each origin's store as
# /origin-<N>. Pelican's https backend takes only one export, so the
# origins export only /protected-a.
#
# The plugin cannot compute checksums, so the suites skip what needs
# them; anything else it can't do fails.

ORIGIN_VARIANT=https
ORIGIN_NAMESPACES=protected-a

fed_enable_profile webdav

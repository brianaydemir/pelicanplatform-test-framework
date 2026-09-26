# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Back the origin with an HTTPS server, through Pelican's native httpsv2
# backend: the `webdav` service, rclone serving
# framework/var/data/origin/<N> over WebDAV. The objects stay plain files,
# so init-data.py writes them as it does for posixv2, and the tests can
# read what the origin stored.
#
# The httpsv2 backend takes only one export, so the origins export only
# /protected-a. The backend asks for no credentials: rclone has no bearer
# tokens.

ORIGIN_VARIANT=httpsv2
ORIGIN_NAMESPACES=protected-a

fed_enable_profile webdav

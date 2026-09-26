# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Turn on posixv2 multiuser (Origin.Multiuser): the origin reads and
# writes each object as the Unix user its token maps to, rather than as
# itself. The users must exist in the origin's image, which has `pelican`
# (uid 10941) and `xrootd` (10940). fed.sh maps the tests' tokens
# (subject pelican-test-framework) to `pelican`, and any other token to
# `xrootd`; a request without a token runs as `nobody`, Pelican's default
# (see framework/var/generated/multiuser-mapfile.json).
#
# Switching users needs root, so fed.sh refuses `server-unprivileged`,
# and any origin but `posixv2`. The test objects are yours, mode 0644, in
# directories any uid may write, so every user can read them. On a Linux
# host, the files the users write are theirs, not yours; ./reset.sh
# removes them.

ORIGIN_MULTIUSER=true

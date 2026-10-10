# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Put XRootD in front of an S3 server (libXrdS3), as the OSDF runs its
# S3 origins: the `s3` service, as for `origin-s3v2`, with each origin's
# store as bucket origin-<N>. Each export maps its federation prefix to
# the bucket's root.
#
# The plugin can neither delete nor compute checksums, so the suites
# skip what needs either; anything else it can't do fails.

ORIGIN_VARIANT=s3

fed_enable_profile s3

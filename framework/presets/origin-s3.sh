# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Put XRootD in front of an S3 server (libXrdS3), as the OSDF runs its
# S3 origins: the `s3` service, as for `origin-s3v2`, but with each
# export's directory of an origin's store as a bucket of its own,
# origin-<N>-<directory>. Each export maps its federation prefix to its
# bucket's root.
#
# The plugin can neither delete nor compute checksums, so the suites
# skip what needs either; anything else it can't do fails.

ORIGIN_VARIANT=s3

fed_enable_profile s3

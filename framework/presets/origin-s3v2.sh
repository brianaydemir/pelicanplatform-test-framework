# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Back the origin with an S3 server, through Pelican's native s3v2
# backend: the `s3` service, rclone serving framework/var/data/origin/<N>
# as bucket origin-<N>. The objects stay plain files, so init-data.py
# writes them as it does for posixv2, and the tests can read what the
# origin stored.
#
# The namespaces share an origin's bucket, as they share a posixv2
# origin's /data. The only checksum is MD5, from the S3 ETag; the origin
# sends no Cache-Control, and has no POSC or metadata publishing. The
# credentials are framework/etc/s3-{access,secret}-key; put others in
# local/etc/ to change them.
#
# rclone's S3 server is lighter than AWS's or Ceph's: it keeps object
# metadata only in memory, and takes a multipart upload's parts only in
# order.

ORIGIN_VARIANT=s3v2

fed_enable_profile s3

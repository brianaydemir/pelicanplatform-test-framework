# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Turn on XRootD's POSC (Origin.EnableAtomicUploads), which stages each
# upload in Origin.UploadTempLocation and renames it into place only when
# it completes. The plugin (libXrdOssPosc, from xrootd-s3-http) serves the
# `posix` origin alone, so this needs `origin-xrootd`; the native origin's
# POSC is `origin-posc`. The posc suite tests it (./fed.sh test posc).
#
# Pelican requires the staging directory to be on the exports' filesystem
# but inside none of them, so fed.sh puts it beside their storage in each
# origin's store: framework/var/data/origin/<N>/.in-progress/, which the
# origin creates (framework/var/generated/conf/60-atomic-uploads.yaml).

ORIGIN_ATOMIC_UPLOADS=true

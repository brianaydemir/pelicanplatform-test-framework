# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Turn on POSC (persist on successful close) in the V2 origin: an upload
# is staged under <StoragePrefix>/.pelican-posc/<user>/ and renamed into
# place only when it completes. Here that is
# framework/var/data/origin/<N>/<namespace>/.pelican-posc/. The posc
# suite tests it (./fed.sh test posc).
#
# Only the `posixv2` origin has it; fed.sh refuses other variants. For
# XRootD's POSC (Origin.EnableAtomicUploads), see `origin-atomic-uploads`.

ORIGIN_POSC=true

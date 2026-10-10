# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Run origin-0 alone, as a standalone origin (Origin.EnableStandaloneMode):
# no discovery host, director, registry, or cache. The origin serves its
# own federation discovery document, so clients reach it as
# pelican://origin-0:8444, and its embedded issuer signs for its exports.
#
# Pelican refuses an XRootD origin (`origin-xrootd`, `origin-s3`,
# `origin-https`) and `origin-no-direct` in this mode, and fed.sh refuses
# whatever adds a server, the caches' XRootD, and the metadata receiver,
# whose verifier needs a registry. The suites skip what needs a director
# or a cache.

TOPOLOGY=standalone

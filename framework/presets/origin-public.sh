# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Have the origins export only /public, as an origin of public data
# does: anyone may read and list every export, no one may write any,
# and none has an issuer, so the origins mint no tokens for their data,
# the directors' redirects name no issuer, and the caches learn none.
# Whatever else chooses the namespaces (`origin-no-direct`,
# `origin-broker`, `origin-https`, and `origin-httpsv2` export one, and
# pick /protected-a), this picks /public: under `origin-no-direct`,
# public data that only the caches may fetch from the origins.
#
# With nothing to write to, and no /protected-a, the suites that need
# either skip, as `./fed.sh test -l` says, among them every suite of the
# data path and of the issuer. The rest test the federation through
# /public: its discovery and redirects, the directors, the registry, the
# advertisements, the clients' reads by every route, and every
# credential's reads, which must all pass, and writes, which must all be
# refused. fed.sh refuses `origin-pstore`, whose /public reads
# /protected-a's storage, `origin-export-templates`, which go on
# /protected-b, and `topo-multi-owner` with `origin-no-direct`, since the
# other owner's /public/secret takes a token beside its public exports,
# which Pelican refuses once the origins serve only caches.

ORIGIN_PUBLIC_ONLY=true

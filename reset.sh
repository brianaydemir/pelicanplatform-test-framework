#!/bin/sh

set -eux

cmd="${1:-}"

rm -rf \
    .condor_creds \
    data/origin \
    data/ads/input
rm -f \
    bin/pelican \
    bin/stash_plugin \
    certs/*.key \
    certs/*.csr \
    certs/*.crt \
    issuer-keys/*.jwks \
    issuer-keys/*.pem

case "${cmd}" in

  init)
    for d in bin certs issuer-keys; do (cd "$d" && ./init.sh); done
    ./init-data.sh
    ;;

esac

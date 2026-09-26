#!/bin/sh
#
# Create the CA and the server certificate every service presents and
# trusts, in framework/var/certs/. Run by `./fed.sh init`.
#
#   init-certs.sh            a new CA and server certificate
#   init-certs.sh --server   only a new server certificate, from the CA
#                            already there, e.g. for a host added to
#                            framework/etc/tls.req
#
# A copy of framework/etc/tls.req goes beside the certificates, so that
# fed.sh can tell when the server certificate lacks a host.

set -eu

cd -- "$(dirname -- "$0")" || exit 1
mkdir -p var/certs
cd var/certs || exit 1

case "${1:-}" in
  "")
      rm -f ./*.key ./*.csr ./*.crt ./*.srl ./tls.req

      # Extensions are explicit because LibreSSL (macOS) and OpenSSL
      # disagree on defaults.
      printf 'Creating the certificate authority ...\n'
      openssl genrsa -out ca.key 4096
      openssl req -new -x509 -sha256 -days 365 -key ca.key -out ca.crt \
          -subj /CN=pelican-test-framework-ca \
          -addext 'basicConstraints = critical, CA:TRUE' \
          -addext 'keyUsage = critical, keyCertSign, cRLSign'
      chmod a-w ca.key ca.crt
      ;;
  --server)
      [ -f ca.key ] && [ -f ca.crt ] \
          || { printf '%s: no CA in framework/var/certs/\n' "$0" >&2; exit 1; }
      rm -f tls.key tls.csr tls.crt tls.req
      ;;
  *)
      printf '%s: takes only --server\n' "$0" >&2
      exit 2
      ;;
esac

printf 'Creating the server certificate ...\n'
openssl genrsa -out tls.key 4096
openssl req -new -sha256 -key tls.key -out tls.csr \
    -subj /CN=pelican-test-framework-server

# -CAcreateserial is required by LibreSSL.
openssl x509 -req -sha256 -days 365 -in tls.csr -out tls.crt \
    -CA ca.crt -CAkey ca.key -CAcreateserial -extfile ../../etc/tls.req
cp ../../etc/tls.req tls.req
chmod a-w tls.key tls.csr tls.crt tls.req

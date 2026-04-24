#!/bin/sh
#
# Create the CA and the server certificate every service presents and
# trusts. Run by `./fed.sh init`.

set -eu

cd -- "$(dirname -- "$0")" || exit 1

rm -f ./*.key ./*.csr ./*.crt ./*.srl

# Extensions are explicit because LibreSSL (macOS) and OpenSSL disagree on
# defaults.
printf 'Creating the certificate authority ...\n'
openssl genrsa -out ca.key 4096
openssl req -new -x509 -sha256 -days 365 -key ca.key -out ca.crt \
    -subj /CN=pelican-test-framework-ca \
    -addext 'basicConstraints = critical, CA:TRUE' \
    -addext 'keyUsage = critical, keyCertSign, cRLSign'
chmod a-w ca.key ca.crt

printf 'Creating the server certificate ...\n'
openssl genrsa -out tls.key 4096
openssl req -new -sha256 -key tls.key -out tls.csr \
    -subj /CN=pelican-test-framework-server

# -CAcreateserial is required by LibreSSL.
openssl x509 -req -sha256 -days 365 -in tls.csr -out tls.crt \
    -CA ca.crt -CAkey ca.key -CAcreateserial -extfile tls.req
chmod a-w tls.key tls.csr tls.crt

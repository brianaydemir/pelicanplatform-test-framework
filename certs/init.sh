#!/bin/sh

set -eux

rm -f ./*.key ./*.csr ./*.crt


# Generate the private key for the CA.
openssl genrsa -out ca.key 4096


# Create the self-signed CA.
openssl req -new -x509 -sha256 -days 365 -key ca.key -out ca.crt -subj "/CN=pelican-test-framework-ca"
chmod a-w ca.key ca.crt


# Generate the private key for the server and the CSR.
openssl genrsa -out tls.key 4096
openssl req -new -sha256 -key tls.key -out tls.csr -subj "/CN=pelican-test-framework-server"


# Sign the server certificate.
openssl x509 -req -sha256 -days 365 -CA ca.crt -CAkey ca.key -in tls.csr -CAcreateserial -out tls.crt -extfile tls.req
chmod a-w tls.key tls.csr tls.crt


# Decode the certificates into human-readable form.
# openssl x509 -in ca.crt -text -noout
# openssl x509 -in tls.crt -text -noout

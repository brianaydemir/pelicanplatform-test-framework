#!/bin/sh

set -eux

rm -f ./*.jwks ./*.pem


#---------------------------------------------------------------------------
# Create service-specific key pairs.

for svc in director registry origin cache; do
  pelican key create --public-key "${svc}.jwks" --private-key "${svc}.pem"
done
chmod a-w ./*.jwks ./*.pem

#!/bin/sh

set -ux  # it's okay if the Pelican client exits with an error

rm -rf data/ads/output ./.condor_creds
mkdir -p data/ads/output ./.condor_creds


#---------------------------------------------------------------------------
# Run a basic sanity check on the federation.
# We need to give caches time to obtain the correct auth config.

while true; do
  if ./bin/pelican object get pelican://discovery:8444/public/data/0.0 /dev/null
  then break
  else sleep 15; fi
done


#---------------------------------------------------------------------------
# Create tokens.

./bin/pelican token create pelican://discovery:8444/private/ \
    --read --scope-path "/" \
    --issuer https://origin-0:8444 --lifetime 1200 --private-key issuer-keys/origin.pem \
    > .condor_creds/run.use


#---------------------------------------------------------------------------
# Run the test.

set +x
printf '\nStarting the test...\n\n'
sleep 2

for x in data/ads/input/*; do
  set -x
  ./bin/stash_plugin -infile "$x" -outfile "data/ads/output/$(basename -- "$x")" &
  set +x
  printf '%b\n' \
      "\e[1mCount of stash_plugin processes: $(pgrep -c stash_plugin)\e[0m"
  sleep 0.25
done

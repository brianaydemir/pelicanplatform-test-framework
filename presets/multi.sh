# shellcheck shell=sh
#
# Add a second origin and a second cache. Both origins export the same
# prefixes; fed.sh pins them to one issuer in
# generated/conf/60-origin-issuers.yaml.

fed_enable_profile multi

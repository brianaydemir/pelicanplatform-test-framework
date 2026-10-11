# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Give the /protected-b export authorization templates of its own
# (AuthorizationTemplates in Origin.Exports), which its issuer applies
# in place of the global Issuer.AuthorizationTemplates: read on
# /data/issuer/export for test-stranger, and nothing else (fed.sh writes
# them). The issuer suite's `templates` scenario checks that
# /protected-b's issuer grants only that, while /protected-a's still
# grants what the global templates say. fed.sh refuses a shape that does
# not export /protected-b (`origin-https`, `origin-httpsv2`,
# `origin-no-direct`, and `origin-broker`), and `auth-external-issuer`,
# under which the origins' issuers are off.

ORIGIN_EXPORT_TEMPLATES=true

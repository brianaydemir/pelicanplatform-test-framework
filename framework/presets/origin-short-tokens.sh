# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Have the origins' embedded issuer mint short-lived tokens: access
# tokens and authorization and device codes last a minute, refresh tokens
# four minutes, and a used refresh token twenty seconds more
# (Issuer.AccessTokenLifetime, Issuer.AuthorizationCodeLifetime,
# Issuer.RefreshTokenLifetime, and Issuer.RefreshTokenGracePeriod), in
# place of an hour, ten minutes, a week, and five minutes. The issuer
# suite's `expiry`, `revoked`, `client`, and `code-expiry` scenarios then
# watch an access token expire and be renewed, a refresh token expire, a
# used refresh token be refused once its grace period is over, the
# Pelican client refresh a token it holds, and codes expire unredeemed.
# Every suite that mints tokens from the issuer uses them, and approves
# each flow, within the minute. fed.sh refuses `auth-no-issuer` and
# `auth-external-issuer`, under which the origins mint no tokens.

ISSUER_SHORT_TOKENS=true

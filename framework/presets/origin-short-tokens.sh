# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Have the origins' embedded issuer mint tokens that soon expire: access
# tokens that last a minute, refresh tokens four minutes, a used refresh
# token twenty seconds more, and authorization and device codes a minute
# (Issuer.AccessTokenLifetime, Issuer.RefreshTokenLifetime,
# Issuer.RefreshTokenGracePeriod, and Issuer.AuthorizationCodeLifetime,
# in place of an hour, a week, five minutes, and ten minutes). The issuer
# suite's `expiry`, `revoked`, `client`, and `code-expiry` scenarios then
# watch an access token expire and be renewed, a refresh token expire, a
# used refresh token be refused once its grace period is over, the
# Pelican client refresh a token it holds, and codes expire unredeemed.
# Every suite that mints tokens from the issuer uses them within the
# minute, and approves each flow within it. fed.sh refuses
# `auth-no-issuer` and `auth-external-issuer`, under which the origins
# mint no tokens.

ISSUER_SHORT_TOKENS=true

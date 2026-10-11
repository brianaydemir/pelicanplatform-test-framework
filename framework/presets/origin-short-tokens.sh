# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Have the origins' embedded issuer mint tokens that soon expire: access
# tokens that last a minute, refresh tokens four minutes, and a used
# refresh token twenty seconds more (Issuer.AccessTokenLifetime,
# Issuer.RefreshTokenLifetime, and Issuer.RefreshTokenGracePeriod, in
# place of an hour, a week, and five minutes). The issuer suite's
# `expiry`, `revoked`, and `client` scenarios then watch an access token
# expire and be renewed, a refresh token expire, a used refresh token be
# refused once its grace period is over, and the Pelican client refresh
# a token it holds. Every suite that mints tokens from the issuer uses
# them within the minute. fed.sh refuses `auth-external-issuer`, under
# which the origins mint no tokens.

ISSUER_SHORT_TOKENS=true

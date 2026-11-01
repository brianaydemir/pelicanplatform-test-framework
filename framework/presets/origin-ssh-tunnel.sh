# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Have the ssh origin reach its helper through the SSH connection itself
# (Origin.SSH.TunnelCallback), as it must where the storage host cannot
# reach the origin. By default, the helper that the origin runs on the
# lab server polls the origin's web URL for requests and connects back to
# it (Pelican's "broker mode"). With this, the helper listens on a Unix
# socket on the lab server, which the origin dials through SSH channels,
# so every request and every byte go over the SSH connection, and nothing
# on the lab server connects out (though here it could). Pelican rates
# the tunnel below a gigabit a second, which the suites' transfers never
# approach. fed.sh writes the setting in
# framework/var/generated/conf/60-ssh-tunnel.yaml.
#
# The suites run as usual. Needs `origin-ssh`; fed.sh refuses any other
# origin.

ORIGIN_SSH_TUNNEL=true

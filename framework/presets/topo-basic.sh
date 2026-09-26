# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# The default topology: each server in a container of its own
# (docker-compose.yaml). For naming the choice explicitly; the
# `topo-multi-*` presets add servers to it, and `topo-tiny` replaces it.

TOPOLOGY=full

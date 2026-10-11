# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Have the registry require an administrator's approval of every
# registration (Registry.RequireOriginApproval and
# Registry.RequireCacheApproval), as the OSDF does: until a namespace or a
# cache is approved, the directors refuse its advertisements, so it serves
# nothing. The federation suite's `ready` scenario approves every pending
# registration through the registry's admin API, as an administrator
# would, and `approval` reports whether any director sent clients to a
# server before then. fed.sh refuses `topo-standalone`, which has no
# registry.

REGISTRY_REQUIRE_APPROVAL=true

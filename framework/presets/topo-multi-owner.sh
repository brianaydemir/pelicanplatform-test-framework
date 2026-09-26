# shellcheck shell=sh
#
# Add origin-2, another owner: an origin with a key and an issuer of its
# own, exporting /other, beside origin-0's prefixes, and /public/other,
# inside origin-0's /public. The directors must route each request by its
# longest matching prefix, and each owner's tokens must be good only in
# its own namespaces. So that origin-2 can register a prefix inside
# origin-0's with its own key, the registry does not require key chaining
# (Registry.RequireKeyChaining), as it does by default.
#
# origin-2 follows ORIGIN_NAMESPACES: /other is like /protected-a, and
# /public/other like /public; nothing is like /protected-b. Its objects
# are 2.<n>, in framework/var/data/origin/2/data/. The owners suite tests
# it (docs/tests.md); the other suites test only the origins' namespaces.
#
# fed.sh refuses `origin-ssh` and `origin-pstore`, which have no store for
# it.

fed_enable_profile multi-owner

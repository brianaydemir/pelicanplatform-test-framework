# shellcheck shell=sh
#
# Add cache-2, a site-local cache (Cache.EnableSiteLocalMode): it
# neither registers nor advertises, so the director never sends a client
# to it, and a client must name it (Client.PreferredCaches). It fetches
# objects through a federated cache, as a client would, presenting the
# client's token, and so needs no federation token, even where the
# origins take only caches (`origin-no-direct`). Its listings go to an
# origin, which refuses them there. The sitelocal suite tests it.

fed_enable_profile site-local-cache

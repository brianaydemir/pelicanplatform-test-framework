#!/bin/sh

set -eux

tarball="pelican-server_Darwin_$(uname -m).tar.gz"
version="7.26.0"

rm -f pelican-server stash_plugin


# Download and extract the tarball.
curl -fSL -O https://github.com/PelicanPlatform/pelican/releases/download/"v${version}"/"${tarball}"
tar xzvf "${tarball}"


# Install the client binaries.
mv "pelican-server-${version}"/pelican-server . ; ln -s pelican-server stash_plugin


# Clean up.
rm -rf "${tarball}" "pelican-server-${version}"

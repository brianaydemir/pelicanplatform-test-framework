#!/bin/sh

set -eux

tarball="pelican_Linux_$(uname -m).tar.gz"
version="7.22.0"

rm -f pelican stash_plugin


# Download and extract the tarball.
curl -fSL -O https://github.com/PelicanPlatform/pelican/releases/download/"v${version}"/"${tarball}"
tar xzvf "${tarball}"


# Install the client binaries.
mv "pelican-${version}"/pelican . ; ln -s pelican stash_plugin


# Clean up.
rm -rf "${tarball}" "pelican-${version}"

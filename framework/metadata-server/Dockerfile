# Pelican's reference receiver for origin metadata events,
# cmd/sample_metadata_server, built from PELICAN_TAG's source: no release
# ships it.

FROM golang:1.26 AS build
ARG PELICAN_TAG
# Fetch a newer toolchain if the tag's go.mod asks for one.
ENV GOTOOLCHAIN=auto
RUN git clone --quiet --depth 1 --branch "${PELICAN_TAG:?}" \
        https://github.com/PelicanPlatform/pelican.git /src \
    && cd /src \
    && CGO_ENABLED=0 go build -buildvcs=false -o /sample_metadata_server \
        ./cmd/sample_metadata_server

FROM alpine:3
COPY --from=build /sample_metadata_server /usr/local/bin/
ENTRYPOINT ["/usr/local/bin/sample_metadata_server"]

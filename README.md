# Pelican Data Federation for Development and Testing

A framework and template for creating a Pelican data federation with some
specified collection of objects, and then running client operations against
the federation's services.


## Dependencies

- Docker and Docker Compose


## Quickstart (for experts)

    ./reset.sh init
    ./fed.sh up

    docker exec -it pelican-dev-1 bash -il

    cd /scratch && ./run.sh


## Quickstart (for non-experts)

Set the directory with this README file as the current working directory.

0. Update `environment.cfg` for your local environment.

1. Download the Pelican client:

        (cd bin && ./init.sh)

    Modify the tarball and version strings in this script as needed.

    This copy is intended to be run inside a Docker container, so be
    mindful of the architecture. (The architecture of the current host
    may differ from that of the container.)

2. Prepare TLS certificates:

        (cd certs && ./init.sh)

3. Prepare server keys:

        (cd issuer-keys && ./init.sh)

    This requires a copy of the `pelican` binary in `$PATH`. (The binary
    from step 1 may be for an architecture that differs from that of the
    current host.)

4. Prepare test data:

        ./init-data.sh

    Modify this script as needed for the desired distribution of objects,
    file transfer plugin input ads, etc.

5. Start (stop, restart) the federation:

        ./fed.sh [up|down|restart]

6. Run the test:

        # Exec into the dev container.
        docker exec -it pelican-dev-1 bash -il

        # Run the test.
        cd /scratch && ./run.sh


## Notable URLs

- https://localhost:8444/: Origin-0 (zero)

    Ports are not mapped for the other origins, if any.

- https://localhost:8445/: Cache-0 (zero)

    Ports are not mapped for the other caches, if any.

- https://localhost:9000/: Director

- https://localhost:9001/: Registry

- http(s)://localhost:9002/: Dev container

    - Nothing is listening here by default
    - Within the container, listen on 0.0.0.0:8444, e.g.,

            go tool pprof -http 0.0.0.0:8444 ...

- http://localhost:9003/: Grafana

    - No `s` in `http`!
    - Username: `admin`
    - Password: `admin` (the default) or `asdf` (what I change it to)


## Configuration

- `environment.cfg`: Container images, location of Pelican's source code

- `etc/pelican.yaml`: Root configuration file for all services and the dev container

- `config.d/*.yaml`: Service-specific configuration

#!/usr/bin/env -S uv run --quiet --frozen --no-dev
"""Create the shape's test objects and the collections the suites write in.

  framework/init-data.py                build them
  framework/init-data.py --print-shape  print the shape and stop

Normally run by `./fed.sh init`, which sets ORIGIN_VARIANT and
COMPOSE_PROFILES from the presets. The shape built is recorded in
framework/var/data/.init-shape, which `./fed.sh up` checks.
"""

# A script, which fed.sh and the docs name as it is.
# pylint: disable=invalid-name

import os
import shutil
import sys
import zlib
from collections.abc import Sequence

# Keep the checkout free of __pycache__.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# pylint: disable=wrong-import-position
from testlib import common, credentials, transfers

# pylint: enable=wrong-import-position

ORIGIN_VARIANT = os.environ.get("ORIGIN_VARIANT") or "posixv2"
COMPOSE_PROFILES = (os.environ.get("COMPOSE_PROFILES") or "").split(",")

# How many objects each origin holds in each namespace (see transfers.LOAD).
OBJECTS = transfers.LOAD.objects
# The namespaces of origin-2, another owner (`topo-multi-owner`), which
# mirror /protected-a and /public; and /public/secret, a protected prefix
# inside /public (fed_owner_exports in fed.sh).
OWNER_NAMESPACES = ("other", "public/other", "public/secret")
# The collections that init makes under each namespace's data/: the
# areas of the suites that write there (testlib/suite.py), and `put`,
# the transfers suite's. Other suites make their own.
SUITE_DIRS = (
    "put",
    "auth",
    "posc",
    "metadata",
    "selftest",
    "commands",
    "blocks",
    "routing",
    "freshness",
    "cache-api",
    "throttling",
    "exports",
    "collections",
    "federation",
    "listings",
    "names",
    "users",
    "owners",
    "sitelocal",
    "tiering",
    "transfer-api",
    "client",
    "restart",
    "keys",
)


def federation_url() -> str:
    """Return the federation's URL, from fed.sh, or the full topology's."""
    try:
        return common.read_text("generated/federation-url")
    except FileNotFoundError:
        return "pelican://discovery:8444"


def shape(exports: transfers.Exports) -> str:
    """Return the shape: everything that changes the data.

    That includes the code that decides it. Other profiles, e.g.
    `multi-cache` and `monitoring`, do not change it.
    """
    used = ",".join(p for p in ("lab", "multi-origin", "multi-owner") if p in COMPOSE_PROFILES) or "none"
    lib = os.path.join(common.FRAMEWORK, "testlib")
    code = b""
    for path in (
        os.path.abspath(__file__),
        os.path.join(lib, "common.py"),
        os.path.join(lib, "credentials.py"),
        os.path.join(lib, "transfers.py"),
    ):
        with open(path, "rb") as f:
            code += f.read()
    exported = ";".join(f"{ns}:{','.join(sorted(caps))}" for ns, caps in sorted(exports.items()))
    return (
        f"origin={ORIGIN_VARIANT} profiles={used} federation={federation_url()}"
        f" exports={exported} objects={OBJECTS} script={zlib.crc32(code)}"
    )


# --------------------------------------------------------------------------
# Objects, named <origin>.<n>, in each namespace's storage: a directory of
# the store (common.storage_dir()), which the s3 service serves as a
# bucket. Only its data/ is replaced, so that a pstore store beside it
# survives. An object's bytes are its path in the federation and a
# newline, so that no two objects are alike, and a store or cache left
# from an earlier `init` still agrees.
#
# The containers write the stores, so this follows no symlink in them
# (see common.open_dir).

# The mode of each directory made: writable by any uid, e.g. `alice` on
# the lab server.
DIR_MODE = 0o777


def remove_tree(name: str, dir_fd: int, shown: str) -> None:
    """Remove directory name in dir_fd, and what it holds, if it is there.

    Args:
        name: The directory.
        dir_fd: Where it is.
        shown: Its path under framework/var, for messages.
    """
    try:
        shutil.rmtree(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    except OSError as e:
        common.die(
            f"cannot remove framework/var/{shown}: {e.strerror or e}; it may hold files a"
            + " container created, or be a symlink; remove it with 'sudo rm -rf --"
            + f" framework/var/{shown}', or start over with ./reset.sh"
        )


def fill_data(data_fd: int, namespace: str, prefixes: Sequence[str]) -> None:
    """Put namespace's objects, and the suites' directories, in data/.

    Args:
        data_fd: The namespace's data/, empty.
        namespace: The namespace.
        prefixes: The objects' names' prefixes: `<origin>` in
            `<origin>.<n>`.
    """
    for prefix in prefixes:
        for n in range(OBJECTS):
            text = f"/{namespace}/data/{prefix}.{n}\n"
            common.write_new_file(f"{prefix}.{n}", text.encode(), 0o644, data_fd)
    # Made here rather than by an origin, whose directories would be root's
    # and so beyond remove_tree() on a Linux host.
    for sub in SUITE_DIRS:
        os.close(common.make_dirs(sub, data_fd, DIR_MODE))


def fill_storage(store: str, store_fd: int, namespace: str, prefixes: Sequence[str]) -> None:
    """Replace namespace's data/ in the store with its objects.

    Also make the directories that the suites write in.

    Args:
        store: The store's directory, under framework/var, for messages.
        store_fd: The store's directory, open.
        namespace: The namespace whose storage to fill.
        prefixes: The objects' names' prefixes: `<origin>` in
            `<origin>.<n>`.
    """
    root = common.storage_dir(namespace)
    root_fd = common.make_dirs(root, store_fd, DIR_MODE)
    try:
        remove_tree("data", root_fd, f"{store}/{root}/data")
        data_fd = common.make_dirs("data", root_fd, DIR_MODE)
        try:
            fill_data(data_fd, namespace, prefixes)
        finally:
            os.close(data_fd)
        # Each put scenario has a collection of its own.
        for scenario in transfers.SCENARIOS:
            if scenario.op == "put" and scenario.namespace == namespace:
                os.close(common.make_dirs(scenario.store_dir, root_fd, DIR_MODE))
    finally:
        os.close(root_fd)


def make_objects(store: str, prefixes: Sequence[str], namespaces: Sequence[str]) -> None:
    """Replace each namespace's data/ in store with its objects.

    Also make the directories that the suites write in.

    Args:
        store: The store's directory, under framework/var (e.g.
            `data/origin/0`), which fed.sh made.
        prefixes: The objects' names' prefixes: `<origin>` in
            `<origin>.<n>`.
        namespaces: The namespaces whose storage to fill.
    """
    try:
        store_fd = common.open_dir(store)
    except OSError as e:
        common.die(f"cannot open framework/var/{store}: {e.strerror or e}")
    try:
        for namespace in namespaces:
            fill_storage(store, store_fd, namespace, prefixes)
    except OSError as e:
        common.die(f"cannot fill framework/var/{store}: {e}")
    finally:
        os.close(store_fd)


def main() -> None:
    """Build the test data, or print the shape it is for."""
    args = sys.argv[1:]
    if args[:1] in (["-h"], ["--help"]):
        print((__doc__ or "").strip("\n"))
        return

    os.makedirs(common.VAR, exist_ok=True)
    common.enter_var()
    exports = common.Federation().exports

    if args == ["--print-shape"]:
        print(shape(exports))
        return
    if args:
        common.die_usage("takes only --print-shape")

    # Where objects go. A pstore origin's store is encrypted, so its
    # objects go beside the store as a plain copy, which the transfers
    # suite uploads; and there, /public reads /protected-a's storage.
    # origin-2, another owner (`topo-multi-owner`), has objects of its own.
    pstore = ORIGIN_VARIANT == "pstore"
    namespaces = list(dict.fromkeys(common.storage_namespace(ns, pstore) for ns in credentials.NAMESPACES))
    prime_origin_0 = ORIGIN_VARIANT != "ssh"
    prime_origin_1 = prime_origin_0 and "multi-origin" in COMPOSE_PROFILES
    prime_origin_2 = prime_origin_0 and "multi-owner" in COMPOSE_PROFILES
    prime_lab = ORIGIN_VARIANT == "ssh" or "lab" in COMPOSE_PROFILES
    skip_reason = "an 'ssh' origin reads the lab server"

    if prime_origin_0:
        print(f"Creating {OBJECTS} objects for origin-0 in each namespace ...")
        make_objects("data/origin/0", ["0"], namespaces)
    else:
        print(f"Skipping origin-0: {skip_reason}.")

    if prime_origin_1:
        # The director may send any request to either origin, so origin-1
        # also gets origin-0's objects.
        print(f"Creating {OBJECTS} objects for origin-1, and origin-0's, in each namespace ...")
        make_objects("data/origin/1", ["1", "0"], namespaces)
    elif "multi-origin" in COMPOSE_PROFILES:
        print(f"Skipping origin-1: {skip_reason}.")

    if prime_origin_2:
        print(f"Creating {OBJECTS} objects for origin-2 in each namespace ...")
        make_objects("data/origin/2", ["2"], OWNER_NAMESPACES)

    if prime_lab:
        # Named like origin-0's, so the same batches work against either.
        print(f"Creating {OBJECTS} objects for the lab server in each namespace ...")
        make_objects("data/lab-server", ["0"], namespaces)

    with open("data/.init-shape", "w", encoding="utf-8") as f:
        f.write(shape(exports) + "\n")
    print(f"Done: {shape(exports)}")


if __name__ == "__main__":
    main()

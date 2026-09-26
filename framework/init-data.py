#!/usr/bin/env python3
"""Create the test objects for the current shape, and the collections
that the test suites write in.

  framework/init-data.py                build them
  framework/init-data.py --print-shape  print the shape and stop

Normally run by `./fed.sh init`, which sets ORIGIN_VARIANT and
COMPOSE_PROFILES from the presets. The shape built is recorded in
framework/var/data/.init-shape, which `./fed.sh up` checks.
"""

import sys

sys.dont_write_bytecode = True

import os  # noqa: E402
import shutil  # noqa: E402
import zlib  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from testlib import common, transfers  # noqa: E402

origin_variant = os.environ.get("ORIGIN_VARIANT") or "posixv2"
profiles = (os.environ.get("COMPOSE_PROFILES") or "").split(",")

# How many objects each origin holds (see transfers.LOAD).
OBJECTS = transfers.LOAD.objects
# The collections that the suites write in, under each store's data/.
SUITE_DIRS = ("put", "auth", "posc", "metadata", "cmd", "blocks", "federation", "listings",
              "users", "owners")


def federation_url() -> str:
    try:
        with open("generated/federation-url") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "pelican://discovery:8444"


def shape(exports: transfers.Exports) -> str:
    """Everything that changes the data, including the code that decides
    it. Other profiles, e.g. `multi-cache` and `monitoring`, do not."""
    used = ",".join(p for p in ("lab", "multi-origin", "multi-owner") if p in profiles) or "none"
    lib = os.path.join(common.FRAMEWORK, "testlib")
    code = b""
    for path in (os.path.abspath(__file__), os.path.join(lib, "credentials.py"),
                 os.path.join(lib, "transfers.py")):
        with open(path, "rb") as f:
            code += f.read()
    exported = ";".join(f"{ns}:{','.join(sorted(caps))}" for ns, caps in sorted(exports.items()))
    return (f"origin={origin_variant} profiles={used} federation={federation_url()}"
            f" exports={exported} objects={OBJECTS} script={zlib.crc32(code)}")


#---------------------------------------------------------------------------
# Objects, named <origin>.<n>. Only the data/ subdirectory is replaced, so
# a pstore store beside it survives. An object's bytes depend only on its
# name, so a store or cache left over from an earlier `init` still agrees.

def make_dir(path: str) -> None:
    # Writable and readable by any uid (e.g. `alice` on the lab server).
    # os.mkdir's mode is subject to the umask, so set it afterward.
    os.mkdir(path)
    os.chmod(path, 0o777)


def make_objects(store: str, prefix: str, objects: int) -> None:
    """Replace store/data with the objects, and the directories that the
    suites write in."""
    directory = f"{store}/data"
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        pass
    except OSError:
        common.die(f"cannot remove framework/var/{directory}, which holds files a container"
                   f" created; remove it with 'sudo rm -rf -- framework/var/{directory}',"
                   " or start over with ./reset.sh")
    os.makedirs(store, exist_ok=True)
    make_dir(directory)
    for n in range(objects):
        path = f"{directory}/{prefix}.{n}"
        with open(path, "w") as f:
            f.write(f"{prefix}.{n}\n")
        os.chmod(path, 0o644)
    # Made here rather than by an origin, whose directories would be root's
    # and so beyond the rmtree above on a Linux host. Each put scenario has
    # a collection of its own.
    for sub in SUITE_DIRS:
        make_dir(f"{directory}/{sub}")
    for scenario in transfers.SCENARIOS:
        if scenario.op == "put":
            make_dir(f"{store}/{scenario.store_dir}")


def main() -> None:
    args = sys.argv[1:]
    if args[:1] in (["-h"], ["--help"]):
        print(__doc__.strip("\n"))
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
    # suite uploads.
    # origin-2, another owner (`topo-multi-owner`), has objects of its own.
    prime_origin_0 = origin_variant != "ssh"
    prime_origin_1 = prime_origin_0 and "multi-origin" in profiles
    prime_origin_2 = prime_origin_0 and "multi-owner" in profiles
    prime_lab = origin_variant == "ssh" or "lab" in profiles
    skip_reason = "an 'ssh' origin reads the lab server"

    if prime_origin_0:
        print(f"Creating {OBJECTS} objects for origin-0 ...")
        make_objects("data/origin/0", "0", OBJECTS)
    else:
        print(f"Skipping origin-0: {skip_reason}.")

    if prime_origin_1:
        print(f"Creating {OBJECTS} objects for origin-1 ...")
        make_objects("data/origin/1", "1", OBJECTS)
        # The director may send any request to either origin, so origin-1
        # also gets origin-0's objects.
        shutil.copytree("data/origin/0/data", "data/origin/1/data", dirs_exist_ok=True)
    elif "multi-origin" in profiles:
        print(f"Skipping origin-1: {skip_reason}.")

    if prime_origin_2:
        print(f"Creating {OBJECTS} objects for origin-2 ...")
        make_objects("data/origin/2", "2", OBJECTS)

    if prime_lab:
        # Named like origin-0's, so the same batches work against either.
        print(f"Creating {OBJECTS} objects for the lab server ...")
        make_objects("data/lab-server", "0", OBJECTS)

    with open("data/.init-shape", "w") as f:
        f.write(shape(exports) + "\n")
    print(f"Done: {shape(exports)}")


if __name__ == "__main__":
    main()

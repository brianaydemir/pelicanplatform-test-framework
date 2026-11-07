#!/usr/bin/env -S uv run --quiet --frozen --no-dev
"""Copy a file to or from a directory that a container may write, safely.

  framework/put-file.py SRC DST     copy SRC to DST
  framework/put-file.py SRC DIR/    copy SRC into DIR, by SRC's name

For fed.sh, which installs binaries and keys where the containers write
(see the dev service in docker-compose.yaml), and for fed.sh and
smoke.sh, which keep what the tests write there. SRC must be a regular
file, not a symlink, and keeps its permission bits. DST is replaced, never
written through: a symlink there is replaced, not followed. SRC's and
DST's directories must be ones that no container can replace, such as a
bind source.
"""

# A script, which fed.sh names as it is.
# pylint: disable=invalid-name

import os
import sys

# Keep the checkout free of __pycache__.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# pylint: disable=wrong-import-position
from testlib import common

# pylint: enable=wrong-import-position


def main() -> None:
    """Copy the file that the arguments name."""
    args = sys.argv[1:]
    if args[:1] in (["-h"], ["--help"]):
        print((__doc__ or "").strip("\n"))
        return
    if len(args) != 2:
        common.die_usage("usage: framework/put-file.py SRC DST (see --help)")
    src, dst = args
    directory, name = os.path.split(dst)
    name = name or os.path.basename(src)
    try:
        data, mode = common.read_file(src)
        dir_fd = common.open_dir(directory or ".")
        try:
            common.replace_file(name, data, mode, dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as e:
        common.die(f"cannot copy {src} to {dst}: {e.strerror or e}")


if __name__ == "__main__":
    main()

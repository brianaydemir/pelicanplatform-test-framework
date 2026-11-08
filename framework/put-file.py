#!/usr/bin/env -S uv run --quiet --frozen --no-dev
"""Copy a file to or from a directory that a container may write, safely.

  framework/put-file.py SRC DST          copy SRC to DST
  framework/put-file.py SRC DIR/         copy SRC into DIR, by SRC's name
  framework/put-file.py --tree SRC DIR   copy each regular file below
                                         SRC to the same place below DIR,
                                         each token's signature cut out

For fed.sh, which installs binaries and keys where the containers write
(see the dev service in docker-compose.yaml), and for fed.sh and
smoke.sh, which keep what the tests write there. SRC must be a regular
file, not a symlink, and keeps its permission bits. DST is replaced, never
written through: a symlink there is replaced, not followed. SRC's and
DST's directories must be ones that no container can replace, such as a
bind source.

--tree is for keeping what the tests write with the logs, which keep no
token whole (see `redact` in framework/report.py). It skips symlinks, and
follows none below SRC, which must not be one itself; DIR and what is
below it are made as needed, and must not be symlinks either.
"""

# A script, which fed.sh names as it is.
# pylint: disable=invalid-name

import os
import sys

# Keep the checkout free of __pycache__.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# pylint: disable=wrong-import-position
from testlib import common, report

# pylint: enable=wrong-import-position


def copy_tree(src_fd: int, dst_fd: int) -> None:
    """Copy each regular file below src_fd to the same place below dst_fd.

    Each token's signature is cut out. Symlinks are skipped, and an entry
    that changes as it is read, as a symlink put in a directory's place,
    is skipped too.
    """
    for entry in os.scandir(src_fd):
        try:
            if entry.is_dir(follow_symlinks=False):
                sub = common.open_dir(entry.name, src_fd)
                try:
                    out = common.make_dirs(entry.name, dst_fd, 0o755)
                    try:
                        copy_tree(sub, out)
                    finally:
                        os.close(out)
                finally:
                    os.close(sub)
            elif entry.is_file(follow_symlinks=False):
                data, mode = common.read_file(entry.name, src_fd)
                text = report.redact(data.decode("utf-8", "surrogateescape"))
                common.replace_file(entry.name, text.encode("utf-8", "surrogateescape"), mode, dst_fd)
        except OSError as e:
            common.warn(f"not keeping {entry.name}: {e.strerror or e}")


def main() -> None:
    """Copy the file, or tree, that the arguments name."""
    args = sys.argv[1:]
    if args[:1] in (["-h"], ["--help"]):
        print((__doc__ or "").strip("\n"))
        return
    if len(args) == 3 and args[0] == "--tree":
        src, dst = args[1:]
        try:
            src_fd = common.open_dir(src)
            try:
                base = common.open_dir("/" if os.path.isabs(dst) else ".")
                try:
                    dst_fd = common.make_dirs(dst, base, 0o755)
                finally:
                    os.close(base)
                try:
                    copy_tree(src_fd, dst_fd)
                finally:
                    os.close(dst_fd)
            finally:
                os.close(src_fd)
        except (OSError, ValueError) as e:
            common.die(f"cannot copy {src} to {dst}: {getattr(e, 'strerror', None) or e}")
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

"""Byte scan every tracked file for U+2014 (em dash) and U+2013 (en dash).

Mirrors and backs the CI dash-scan step; exit 1 lists the offenders.
"""

import subprocess
import sys

NEEDLES = (
    ("em dash", b"\xe2\x80\x94"),
    ("en dash", b"\xe2\x80\x93"),
)


def main() -> int:
    out = subprocess.run(["git", "ls-files", "-z"], check=True,
                         capture_output=True).stdout
    paths = [f.decode("utf-8") for f in out.split(b"\0") if f]
    offenders = []
    for name in paths:
        with open(name, "rb") as handle:
            data = handle.read()
        for tag, needle in NEEDLES:
            offset = data.find(needle)
            if offset != -1:
                offenders.append("%s: %s at byte %d" % (name, tag, offset))
    if offenders:
        print("\n".join(offenders), file=sys.stderr)
        return 1
    print("dash scan: 0 hits across %d tracked files" % len(paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())

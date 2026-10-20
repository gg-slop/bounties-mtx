#!/usr/bin/env python3
"""Encode or decode a JSONL file one line at a time using Base64."""

import argparse
import base64
import os
import tempfile


def main():
    """Encode PATH to PATH.b64, or decode PATH.b64 back to PATH."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("encode", "decode"))
    parser.add_argument("path", help="input path (without .b64 when decoding)")
    args = parser.parse_args()

    if args.mode == "encode":
        src, dst = args.path, args.path + ".b64"
    else:
        src, dst = args.path + ".b64", args.path

    parent = os.path.dirname(os.path.abspath(dst))
    fd, temp = tempfile.mkstemp(prefix=".jsonl-base64-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as fout:
            with open(src, "rb") as fin:
                for line in fin:  # Bytes split on LF only, never on U+2028, etc.
                    line = line.rstrip(b"\n")
                    if not line:
                        continue
                    if args.mode == "encode":
                        fout.write(base64.b64encode(line) + b"\n")
                    else:
                        fout.write(base64.b64decode(line, validate=True) + b"\n")
        os.replace(temp, dst)
    except BaseException:
        if os.path.exists(temp):
            os.unlink(temp)
        raise

    print(f"{args.mode}d {src} -> {dst}")


if __name__ == "__main__":
    main()

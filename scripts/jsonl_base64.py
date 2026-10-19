#!/usr/bin/env python3
"""Encode or decode a JSONL file one line at a time using Base64."""

import argparse
import base64


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

    with open(src, "rb") as fin, open(dst, "wb") as fout:
        for line in fin:  # Bytes split on LF only, never on U+2028, etc.
            line = line.rstrip(b"\n")
            if not line:
                continue
            if args.mode == "encode":
                fout.write(base64.b64encode(line) + b"\n")
            else:
                fout.write(base64.b64decode(line, validate=True) + b"\n")

    print(f"{args.mode}d {src} -> {dst}")


if __name__ == "__main__":
    main()

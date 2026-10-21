#!/usr/bin/env bash
# Pick the submission files out of a PR's changed-file list.
#
#   list_submissions.sh CHANGED OUT
#
# CHANGED has one JSON-quoted (`tojson`) file name per line, so a newline or
# quote inside a name stays visible and fails the strict name pattern instead of
# splitting a line. OUT receives the valid rooms/<ROOM>/submissions/<name> paths,
# unique and in numeric part order. Exits 1 on a malformed submission name or on
# more than one logical submission in a room.
set -euo pipefail

changed=${1:?usage: list_submissions.sh CHANGED OUT}
out=${2:?usage: list_submissions.sh CHANGED OUT}

name='([A-Za-z0-9_@+=-]+|([A-Za-z0-9_@+=-]+\.)+[A-Za-z0-9_@+=-]*[A-Za-z_@+=-][A-Za-z0-9_@+=-]*)(\.[0-9]{2,})?\.jsonl\.b64'
valid="^\"rooms/[A-Za-z0-9_-]{43}/submissions/$name\"\$"

grep -E "$valid" "$changed" | tr -d '"' > "$out" || true
if grep -E 'submissions/.*jsonl\.b64"$' "$changed" | grep -vE "$valid"; then
  echo "invalid submission file name (see the room README)" >&2
  exit 1
fi

# -V orders numbered parts numerically (.11 before .100).
sort -u "$out" -o "$out"
sort -V "$out" -o "$out"

# One logical submission per room: numbered parts of a single name.
multi=$(sed -E 's/(\.[0-9]+)?\.jsonl\.b64$//' "$out" | sort -u | cut -d/ -f2 | uniq -d)
if [[ -n "$multi" ]]; then
  echo "more than one submission name in room(s): $multi" >&2
  exit 1
fi

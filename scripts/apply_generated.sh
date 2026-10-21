#!/usr/bin/env bash
# Swap a PR's submissions for the deltas generated from them, staged in git.
#
#   apply_generated.sh SUBMITTED GENERATED
#
# Run from the PR checkout. SUBMITTED lists the submission paths to remove, one
# per line. GENERATED is the untrusted build artifact: it may hold nothing but
# rooms/<ROOM>/deltas/<regular files>, and every destination must not be a
# symlink. Stages the removals and additions; the caller commits and pushes.
set -euo pipefail

submitted=${1:?usage: apply_generated.sh SUBMITTED GENERATED}
generated=${2:?usage: apply_generated.sh SUBMITTED GENERATED}

while IFS= read -r s; do git rm -q -- "$s"; done <"$submitted"

for top in "$generated"/*; do
	[[ "${top##*/}" == rooms ]] || {
		echo "unexpected generated path: ${top##*/}" >&2
		exit 1
	}
done

for room_dir in "$generated"/rooms/*; do
	room=${room_dir##*/}
	[[ "$room" =~ ^[A-Za-z0-9_-]{43}$ ]] || {
		echo "unexpected generated room path: $room" >&2
		exit 1
	}
	if [[ -L rooms || -L "rooms/$room" || -L "rooms/$room/deltas" ]]; then
		echo "rooms/$room/deltas: refusing symlink destination" >&2
		exit 1
	fi
	mkdir -p "rooms/$room/deltas"
	for delta in "$room_dir"/deltas/*; do
		[[ -f "$delta" && ! -L "$delta" ]] || {
			echo "$delta: expected a regular generated file" >&2
			exit 1
		}
		target="rooms/$room/deltas/${delta##*/}"
		[[ ! -L "$target" ]] || {
			echo "$target: refusing symlink destination" >&2
			exit 1
		}
		cp -- "$delta" "$target"
		git add -- "$target"
	done
done

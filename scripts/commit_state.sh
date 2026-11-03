#!/usr/bin/env bash
# Commit the named state files and push them WITHOUT discarding this run's copy.
#
# WHY THIS EXISTS: every state step used to end with
#
#     git pull --ff-only || git pull --rebase -X theirs
#
# and `-X theirs` resolves a conflicting rebase by taking the REMOTE side. On
# 2026-11-01 the 08:14 and 08:27 breaking sweeps raced: the slower run's
# state/model_health.json increments lost, so the record the ladder reorders
# around was silently rewritten by whatever the other run had. The same shape
# existed on the cooldown file and the alert ledger.
#
# The rule here: a race is retried, never resolved by deletion. When the remote
# has moved, this run's files are re-applied on top of the fresh remote tip.
# The trade-off is stated rather than hidden: for a file BOTH runs rewrote, the
# remote's version of that file is superseded by this run's copy, and a warning
# says so.
#
# The re-apply works on the INDEX, never with `reset --hard`: the workspace is
# shared with earlier steps, and a hard reset would throw away their
# uncommitted work as a side effect of saving ours.
#
# Usage: commit_state.sh "<message>" <path> [<path> ...]
# Exit 0 when the state is pushed, already present, or unchanged. Exit 0 even
# after the retries are exhausted -- losing telemetry must not fail the job --
# but the warning names it.
set -uo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: commit_state.sh <message> <path> [<path> ...]" >&2
  exit 2
fi

message="$1"
shift
files=("$@")
branch="${GITHUB_REF_NAME:-master}"

# state/ladder_probe.json IS A PROBE'S RESULT, and only a probe run may write
# it. On 2026-11-02 a concurrent run whose checkout still contained the
# committed file staged it with the rest of its state and rewrote the probe's
# answer 36s after the probe produced it. The caller already guards the flag;
# this is the same rule at the only place that can stage anything, so a future
# caller cannot re-introduce the clobber by adding the path back.
if [ "${LADDER_PROBE:-false}" != "true" ]; then
  kept=()
  for f in "${files[@]}"; do
    if [ "$f" = "state/ladder_probe.json" ]; then
      echo "commit_state: refusing $f -- this run was not the ladder probe"
    else
      kept+=("$f")
    fi
  done
  files=("${kept[@]+"${kept[@]}"}")
  if [ "${#files[@]}" -eq 0 ]; then
    echo "commit_state: nothing but the probe file was given; nothing to commit"
    exit 0
  fi
fi

git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"

# The checkout persists no credentials, so the token is injected for the
# network commands only, via `git -c`.
auth=()
if [ -n "${GITHUB_TOKEN:-}" ]; then
  auth=(-c "http.https://github.com/.extraheader=AUTHORIZATION: basic $(printf 'x-access-token:%s' "$GITHUB_TOKEN" | base64 -w0)")
else
  echo "::warning::GITHUB_TOKEN is empty -- pull/push will run unauthenticated"
fi

# Stage only paths that exist or are still tracked (a deleted cooldown file
# must commit its deletion). Staging is by explicit name: `git add -A` or
# `git add state/` would sweep in every preview file the workspace holds.
staged=()
for f in "${files[@]}"; do
  if [ -f "$f" ] || git ls-files --error-unmatch -- "$f" >/dev/null 2>&1; then
    git add -- "$f"
    staged+=("$f")
  fi
done
if [ "${#staged[@]}" -eq 0 ]; then
  echo "commit_state: none of the given paths exist; nothing to commit"
  exit 0
fi
if git diff --staged --quiet -- "${staged[@]}"; then
  echo "commit_state: no changes to commit"
  exit 0
fi

git commit -m "$message" -- "${staged[@]}" >/dev/null
ours=$(git rev-parse HEAD)
echo "commit_state: committed $ours"

for attempt in 1 2 3; do
  if ! git "${auth[@]}" fetch --quiet origin "$branch"; then
    sleep $((attempt * 2))
    continue
  fi
  base=$(git rev-parse "origin/$branch")

  if git merge-base --is-ancestor "$base" HEAD 2>/dev/null; then
    if git "${auth[@]}" push --quiet origin "HEAD:$branch"; then
      echo "commit_state: pushed on attempt $attempt"
      exit 0
    fi
    echo "::warning::commit_state: push rejected on attempt $attempt (the remote moved); retrying"
  else
    # THE REMOTE MOVED UNDER US. Rebuild this run's commit on the fresh tip
    # using only the index; the rest of the working tree is left as it was.
    echo "::warning::commit_state: attempt $attempt: origin/$branch moved; re-applying this run's copy of ${staged[*]} over the fresh remote tip (the remote's concurrent rows in those files are superseded)"
    if ! git read-tree "$base"; then
      sleep $((attempt * 2))
      continue
    fi
    for f in "${staged[@]}"; do
      if git cat-file -e "$ours:$f" 2>/dev/null; then
        git checkout "$ours" -- "$f" || continue 2
      else
        git update-index --force-remove -- "$f" 2>/dev/null || true
      fi
    done
    tree=$(git write-tree) || { sleep $((attempt * 2)); continue; }
    if [ "$tree" = "$(git rev-parse "$base^{tree}")" ]; then
      echo "commit_state: the remote already carries this state"
      exit 0
    fi
    new=$(git commit-tree "$tree" -p "$base" -m "$message") \
      || { sleep $((attempt * 2)); continue; }
    if git "${auth[@]}" push --quiet origin "$new:$branch"; then
      echo "commit_state: pushed on attempt $attempt after re-applying"
      if [ "$(git symbolic-ref --quiet --short HEAD 2>/dev/null)" = "$branch" ]; then
        git update-ref "refs/heads/$branch" "$new"
        git reset --mixed "$new" >/dev/null 2>&1 || true
      fi
      exit 0
    fi
    echo "::warning::commit_state: push rejected on attempt $attempt after re-applying"
  fi
  sleep $((attempt * 2))
done

echo "::warning::commit_state: gave up after 3 attempts -- this run's state was not pushed; the next sweep rewrites its own rows"
exit 0

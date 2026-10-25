#!/usr/bin/env bash
# Test stub for the OpenCode CLI.
#
# WHY THIS EXISTS
# ---------------
# Bug A (2026-09-27): the flag gate in daily-news.yml / breaking-monitor.yml /
# drift-canary.yml piped `opencode run --help` into `grep -q` on STDOUT.
# The real CLI had moved `--help` to STDERR, so a working `--auto` flag read
# as missing, the gate printed
#   FATAL: opencode run lacks --auto (config/opencode.json flags)
# and the daily digest died at its pre-spend gate.  425 Python tests stayed
# green throughout, because nothing invoked the gate.
#
# This stub makes that testable offline.  It is a real executable shell
# script -- not a reimplementation of the gate -- so the regression test
# exercises the exact shell the workflow runs.  The gate is extracted from
# the parsed workflow YAML at test time, never hand-copied.
#
# Contract, read from the environment so one stub covers every case:
#   STUB_HELP_STREAM   stderr (default) | stdout | both
#   STUB_HELP_FLAGS    space-separated flags to advertise in --help
#   STUB_VERSION       version string reported by --version (default 1.2.3)
#
# Any other argument prints usage and exits 2, so an unexpected invocation
# surfaces as a loud failure instead of a silent success.

set -u

HELP_STREAM="${STUB_HELP_STREAM:-stderr}"
HELP_FLAGS="${STUB_HELP_FLAGS:---auto --model}"
VERSION="${STUB_VERSION:-1.2.3}"

if [ "${1:-}" = "--version" ]; then
  printf 'opencode %s\n' "$VERSION"
  exit 0
fi

if [ "${1:-}" = "run" ] && [ "${2:-}" = "--help" ]; then
  # Build the help text, then emit it on the stream the real CLI uses.
  # Bug A lived precisely in the difference between these two branches.
  help="$(
    printf 'Usage: opencode run [options]\n'
    printf '\nOptions:\n'
    for flag in $HELP_FLAGS; do
      printf '  %s  (flag advertised by this stub)\n' "$flag"
    done
    printf '  -h, --help  Show this help and exit\n'
  )"
  case "$HELP_STREAM" in
    stdout) printf '%s\n' "$help" ;;
    both)   printf '%s\n' "$help" >&2; printf '%s\n' "$help" ;;
    stderr) printf '%s\n' "$help" >&2 ;;
    *)      echo "STUB_OPENCODE ERROR: bad STUB_HELP_STREAM=$HELP_STREAM" >&2; exit 3 ;;
  esac
  exit 0
fi

echo "STUB_OPENCODE ERROR: unexpected invocation: $*" >&2
exit 2

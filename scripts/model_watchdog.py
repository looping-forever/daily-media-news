#!/usr/bin/env python3
"""model_watchdog.py -- abandon an attempt the moment it is known to be dead.

WHY THIS EXISTS
---------------
`opencode run` has no timeout of its own, so the walk bounded it with GNU
`timeout` and a 360s per-attempt cap. That cap is generous because a WORKING
model has been measured at ~300s to finish -- but it is also what a DEAD model
costs.

Two different deaths were measured on 2026-10-25, and they need two triggers:

1. A DECISIVE ERROR. The provider rejected the call 1.0s in:

       15:42:09.535 stream error error.error="AI_APICallError: Free usage limit reached."

   The CLI printed that line and then NEVER EXITED. Two models, both rejected at
   +1s, both burned their full 480s cap producing zero tokens. So the stream is
   classified as it arrives and the attempt is killed as soon as a decisive line
   appears. Prose can never trigger this: a line must be error-shaped BEFORE its
   class is even considered (see model_selector.classify_stream_line), because a
   digest article can legitimately contain "rate limit" or "quota".

2. SILENCE. Every live free model answered with its first token in 12-20s (and a
   control run at 20.9s). An attempt that has printed NOTHING for 90s is not
   slow, it is stuck, so 90s is more than four times the slowest measured first
   token and sits safely above it.

    cat prompt | timeout 360 python3 scripts/model_watchdog.py \
        --no-output-sec 90 -- opencode run --auto --model <id>

Exit codes: the child's own code; 124 when killed for silence (the same code
`timeout` uses, so the caller's existing rc=124 branch keeps its meaning); 125
when killed for a decisive error line.
"""

import os
import subprocess
import sys
import threading
import time

MARKER = "__WATCHDOG_NO_OUTPUT__"
FATAL_MARKER = "__WATCHDOG_FATAL__"
#: Exit code for "killed because the stream said the attempt is finished".
FAST_FAIL_RC = 125

#: Matches NO_OUTPUT_WATCHDOG_SEC in model_selector.py. Duplicated as a CLI
#: default only because this file must not DEPEND on importing the selector to
#: run: if the import below fails, silence-only supervision still has to work.
DEFAULT_NO_OUTPUT_SEC = 90
POLL_SEC = 0.1

#: The classifier is the SINGLE source of truth for what a decisive line is, so
#: the watchdog and the shell's post-hoc `--classify` can never disagree. A
#: missing module degrades to silence-only supervision and SAYS SO, rather than
#: silently losing the fast-fail path.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from model_selector import classify_stream_line  # noqa: E402
except Exception:                                          # pragma: no cover
    classify_stream_line = None


def _reader(stream, sink, state):
    """Drain the child's merged output: tee it, and look for a decisive line."""
    try:
        for line in stream:
            state["last"] = time.monotonic()
            sink.write(line)
            sink.flush()
            if state["fatal"] is None and classify_stream_line is not None:
                kind = classify_stream_line(line)
                if kind:
                    state["fatal"] = (kind, line.strip())
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _kill(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def supervise(cmd, no_output_sec=DEFAULT_NO_OUTPUT_SEC, sink=None):
    """Run `cmd`; return its code, 124 if it went silent, 125 if it was rejected."""
    out = sink or sys.stdout
    if classify_stream_line is None:
        print("model_watchdog: classifier unavailable; supervision is "
              "silence-only (%ss)" % no_output_sec, file=sys.stderr)
    proc = subprocess.Popen(
        list(cmd), stdin=sys.stdin, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, errors="replace",
        bufsize=1, encoding="utf-8")
    state = {"last": time.monotonic(), "fatal": None}
    reader = threading.Thread(target=_reader, args=(proc.stdout, out, state),
                              daemon=True)
    reader.start()

    try:
        while proc.poll() is None:
            if state["fatal"] is not None:
                kind, line = state["fatal"]
                _kill(proc)
                out.write("%s kind=%s :: %s\n" % (FATAL_MARKER, kind, line))
                out.flush()
                reader.join(timeout=5)
                return FAST_FAIL_RC
            if time.monotonic() - state["last"] > float(no_output_sec):
                _kill(proc)
                out.write("%s no output for %ss; attempt abandoned\n"
                          % (MARKER, no_output_sec))
                out.flush()
                reader.join(timeout=5)
                return 124
            time.sleep(POLL_SEC)
    except KeyboardInterrupt:
        proc.kill()
        raise

    # The child exited on its own. Drain the reader so the caller's log cannot
    # lose the tail that explains the failure.
    reader.join(timeout=5)
    return proc.returncode


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    no_output, cmd = DEFAULT_NO_OUTPUT_SEC, []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--no-output-sec" and i + 1 < len(argv):
            try:
                no_output = int(argv[i + 1])
            except ValueError:
                print("model_watchdog: --no-output-sec must be an integer",
                      file=sys.stderr)
                return 2
            i += 2
        elif arg == "--":
            cmd = argv[i + 1:]
            break
        else:
            print("model_watchdog: unknown argument %r (expected "
                  "--no-output-sec N -- CMD...)" % arg, file=sys.stderr)
            return 2
    if not cmd:
        print("model_watchdog: no command after --", file=sys.stderr)
        return 2
    return supervise(cmd, no_output)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""model_watchdog.py -- abandon an attempt the moment it is known to be dead.

WHY THIS EXISTS
---------------
`opencode run` has no timeout of its own, so the walk bounded it with GNU
`timeout` and a 360s per-attempt cap. That cap is generous because a WORKING
model has been measured at ~128s to finish -- but it is also what a DEAD model
costs.

Three different deaths were measured, and they need three triggers:

1. A DECISIVE ERROR. The provider rejected the call 1.0s in:

       15:42:09.535 stream error error.error="AI_APICallError: Free usage limit reached."

   The CLI printed that line and then NEVER EXITED, so two models burned their
   full cap producing zero tokens. The stream is classified as it arrives and
   the attempt is killed as soon as a decisive line appears. Prose can never
   trigger this: a line must be error-shaped BEFORE its class is considered
   (see model_selector.classify_stream_line), because a digest article can
   legitimately contain "rate limit" or "quota".

2. SILENCE -- but per model, because one window cannot serve two models whose
   measured time-to-first-token differs by a factor of nine:

       deepseek-v4.1-flash-free   14.59s  -> 60s window
       beam-free                 137.63s  -> 240s window
       everything else                      60s window

   60s for beam would make the only other model that has ever produced a digest
   permanently unreachable, which is why the table lives in model_selector.py
   (NO_OUTPUT_BY_MODEL) and is read by name here.

   AND THE TRAP, so a quiet attempt is never read as a dead one: stdout carries
   assistant TEXT only. The probe watched nemotron and glm stream TOOL CALLS
   with no text at all, so "no output" means "no assistant text yet". These
   windows are the only silence signal.

3. A REPEATING OUTAGE. flint-40b printed the identical line

       AI_APICallError: Upstream request failed: Endpoint is unavailable.

   five times from 12.6s, produced zero tokens, and still burned the allowance.
   A single 5xx must keep its chance (the CLI can recover), so this is a
   COUNTER, not a marker: REPEAT_OUTAGE_LIMIT identical fatal lines with zero
   assistant text means the endpoint is gone. See model_selector for the rule.

    cat prompt | timeout 360 python3 scripts/model_watchdog.py \
        --model <id> -- opencode run --auto --model <id>

Exit codes: the child's own code; 124 when killed for silence (the same code
`timeout` uses, so the caller's existing rc=124 branch keeps its meaning); 125
when killed for a decisive line or a repeating outage.
"""

import os
import re
import subprocess
import sys
import threading
import time

MARKER = "__WATCHDOG_NO_OUTPUT__"
FATAL_MARKER = "__WATCHDOG_FATAL__"
#: Exit code for "killed because the stream said the attempt is finished".
FAST_FAIL_RC = 125

#: Last-resort window when the selector cannot be imported AND no model was
#: named. Matches NO_OUTPUT_DEFAULT_SEC, and duplicated only so this file still
#: supervises something if that import fails.
DEFAULT_NO_OUTPUT_SEC = 60
POLL_SEC = 0.1

#: The classifier and the window table are SINGLE-SOURCED in model_selector, so
#: the watchdog and the shell's post-hoc `--classify` can never disagree. A
#: missing module degrades to silence-only supervision and SAYS SO, rather than
#: silently losing the fast-fail path.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from model_selector import (  # noqa: E402
        REPEAT_OUTAGE_LIMIT,
        classify_failure,
        classify_stream_line,
        is_error_shaped,
        no_output_sec_for,
    )
except Exception:                                          # pragma: no cover
    classify_stream_line = classify_failure = is_error_shaped = None
    no_output_sec_for = None
    REPEAT_OUTAGE_LIMIT = 3

#: The CLI prefixes log lines with a clock. Two reports of the same dead
#: endpoint differ only by that prefix, so it is stripped before comparison.
_TS_RE = re.compile(r"^\s*\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\s*")


def resolve_window(model=None, explicit=None):
    """The silence window to enforce: an explicit number wins, else the table."""
    if explicit is not None:
        return int(explicit)
    if model and no_output_sec_for is not None:
        return int(no_output_sec_for(model))
    return DEFAULT_NO_OUTPUT_SEC


def _normal_key(line):
    """The same fatal line, minus the timestamp the CLI prefixes."""
    return " ".join(_TS_RE.sub("", (line or "").lower()).split())


def _note_fatal_line(state, line):
    """Count an identical fatal line; True when it is a repeating outage.

    `state["text"]` is set the moment any assistant text appears, because an
    endpoint that keeps failing while the model has ALREADY produced text is a
    partially working model, not a dead one -- and killing it would throw away
    work in progress. That is the "zero assistant tokens" half of the rule.
    """
    if state["text"]:
        return False
    key = _normal_key(line)
    if not key:
        return False
    state["outage"][key] = state["outage"].get(key, 0) + 1
    return state["outage"][key] >= int(REPEAT_OUTAGE_LIMIT)


def _reader(stream, sink, state):
    """Drain the child's merged output: tee it, classify it, count repeats."""
    try:
        for line in stream:
            state["last"] = time.monotonic()
            sink.write(line)
            sink.flush()
            if classify_stream_line is None:
                continue
            # Assistant TEXT is the only visible sign of life on this stream,
            # so anything that is not an error/log line counts as text.
            if not state["text"] and line.strip() and not is_error_shaped(line):
                state["text"] = True
            if state["fatal"] is None:
                kind = classify_stream_line(line)
                if kind:
                    state["fatal"] = (kind, line.strip())
                    continue
            if state["repeat"] is None and state["fatal"] is None:
                on_line = classify_failure(line, 1)
                if on_line == "provider_outage" and is_error_shaped(line):
                    if _note_fatal_line(state, line):
                        state["repeat"] = (line.strip(),
                                           state["outage"][_normal_key(line)])
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


def supervise(cmd, no_output_sec=None, sink=None, model=None):
    """Run `cmd`; 124 if it went silent, 125 if it was rejected or repeating."""
    out = sink or sys.stdout
    window = resolve_window(model=model, explicit=no_output_sec)
    if classify_stream_line is None:
        print("model_watchdog: classifier unavailable; supervision is "
              "silence-only (%ss)" % window, file=sys.stderr)
    proc = subprocess.Popen(
        list(cmd), stdin=sys.stdin, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, errors="replace",
        bufsize=1, encoding="utf-8")
    state = {"last": time.monotonic(), "fatal": None, "repeat": None,
             "text": False, "outage": {}}
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
            if state["repeat"] is not None:
                line, seen = state["repeat"]
                _kill(proc)
                out.write("%s kind=provider_outage repeats=%d :: %s\n"
                          % (FATAL_MARKER, seen, line))
                out.flush()
                reader.join(timeout=5)
                return FAST_FAIL_RC
            if time.monotonic() - state["last"] > float(window):
                _kill(proc)
                out.write("%s no output for %ss; attempt abandoned\n"
                          % (MARKER, window))
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
    explicit, model, cmd = None, None, []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--no-output-sec" and i + 1 < len(argv):
            try:
                explicit = int(argv[i + 1])
            except ValueError:
                print("model_watchdog: --no-output-sec must be an integer",
                      file=sys.stderr)
                return 2
            i += 2
        elif arg == "--model" and i + 1 < len(argv):
            model = argv[i + 1]
            i += 2
        elif arg == "--":
            cmd = argv[i + 1:]
            break
        else:
            print("model_watchdog: unknown argument %r (expected "
                  "--model ID | --no-output-sec N -- CMD...)" % arg,
                  file=sys.stderr)
            return 2
    if not cmd:
        print("model_watchdog: no command after --", file=sys.stderr)
        return 2
    window = resolve_window(model=model, explicit=explicit)
    # Stated once, in the attempt log, so the window that was enforced is
    # auditable afterwards instead of being inferred from the file's constants.
    print("model_watchdog: window=%ss model=%s" % (window, model or "?"),
          file=sys.stderr)
    return supervise(cmd, no_output_sec=window)


if __name__ == "__main__":
    sys.exit(main())

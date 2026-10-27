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

2. SILENCE -- in TWO windows, because time-to-first-token and the gap between
   tokens are different questions and one number cannot answer both. WHICH
   CLOCK THOSE WINDOWS READ WAS WRONG UNTIL 2026-10-27:

       STDOUT IS NOT A LIVENESS SIGNAL. In non-TTY mode `opencode run` prints a
       banner (`> build · <model>`) at ~5s and then NOTHING until the whole
       assistant message is complete -- measured: 90 lines arriving in one burst
       at +62.89s. So the ONLY thing stdout can prove is COMPLETION. It is
       recorded as such and otherwise ignored.

       LIVENESS COMES FROM THE CLI'S OWN LOG. With `--print-logs --log-level
       DEBUG` on stderr the CLI narrates every phase as it happens:

           timestamp=... level=INFO  message=stream providerID=opencode modelID=...
           timestamp=... level=INFO  message="llm runtime selected" ...
           timestamp=... level=INFO  message=loop ... step=N

       Those lines advance the clock. Nothing else does.

   THE BUG THIS FIXES, measured on seven models. The banner used to count as
   "assistant text seen" (it is neither an error nor a log line), which switched
   the attempt from the per-model FIRST-OUTPUT window to the 240s inter-token
   window at +5s and made the first-output window dead code. That is why seven
   different models "stalled" at exactly 240s: ONE artefact, not seven dead
   models. The banner is now stripped (ANSI too) and rejected by pattern before
   any `line.strip()` test can see it.

       FIRST OUTPUT (the model call has not opened yet), per model, from the
       measurement:

           deepseek-v4.1-flash-free   14.59s  -> 60s window
           beam-free                 137.63s  -> 240s window
           everything else                      60s window

       60s for beam would make the only other model that has ever produced a
       digest permanently unreachable, which is why that table lives in
       model_selector.py (NO_OUTPUT_BY_MODEL) and is read by name here. This
       window only has to cover CLI startup and provider dispatch (measured:
       13.57s to `message=stream`), because the phase lines above carry the rest.

       INTER TOKEN (the call has opened), one generous number:

           INTER_TOKEN_SEC = 420s

   420s and not 240s: the inter-token window now measures LOG silence across a
   whole generation, and generation is where the logs are quiet -- the CLI
   prints nothing between `llm runtime selected` and `loop step=1`. The measured
   completion for this workload is 127.53s, CI has been observed past 240s, and
   a window that fires before completion is the defect, not the protection. The
   per-attempt cap (480s) stays the real bound on a slow attempt.

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
#: Last-resort inter-token window, for the same reason. Matches INTER_TOKEN_SEC.
DEFAULT_INTER_TOKEN_SEC = 240
POLL_SEC = 0.1

#: The classifier and the window table are SINGLE-SOURCED in model_selector, so
#: the watchdog and the shell's post-hoc `--classify` can never disagree. A
#: missing module degrades to silence-only supervision and SAYS SO, rather than
#: silently losing the fast-fail path.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from model_selector import (  # noqa: E402
        INTER_TOKEN_SEC,
        REPEAT_OUTAGE_LIMIT,
        classify_failure,
        classify_stream_line,
        is_error_shaped,
        no_output_sec_for,
    )
except Exception:                                          # pragma: no cover
    classify_stream_line = classify_failure = is_error_shaped = None
    no_output_sec_for = None
    INTER_TOKEN_SEC = DEFAULT_INTER_TOKEN_SEC
    REPEAT_OUTAGE_LIMIT = 3

#: The CLI prefixes log lines with a clock. Two reports of the same dead
#: endpoint differ only by that prefix, so it is stripped before comparison.
_TS_RE = re.compile(r"^\s*\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\s*")
#: Colour codes the CLI writes around the banner. Stripped first, so a pattern
#: test never has to know about them.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
#: The non-TTY banner: `> build · <model>`. It is decoration, it arrives ~5s in,
#: and counting it as assistant text is what made every model look stalled at
#: exactly the inter-token window. The separator is matched as ANY short
#: non-word run, not as U+00B7: under a non-UTF-8 locale (Actions runs a bare
#: `LANG` in places) the `·` arrives as a replacement character, and a pattern
#: tied to the literal would let the banner through and re-arm the whole defect.
#: A markdown blockquote cannot match, because its second token is a word.
_BANNER_RE = re.compile(r"^>\s+\S+\s+[^\w\s]+\s*")
#: A CLI log line: `timestamp=… level=… run=… message=…`. Any of them proves the
#: process is alive; these are the ONLY liveness signal there is.
_LOG_RE = re.compile(r"\blevel=(?:DEBUG|INFO|WARN|ERROR)\b.*\bmessage=")
#: A `message=stream` line means a model call was opened -- but the CLI opens a
#: SMALL one first (title generation, `small=true`, a different model) at ~+5s,
#: measured 2026-10-27. Counting that as the main call put every attempt on the
#: generous window five seconds in, before the model it was chosen for had been
#: contacted at all. Only the non-small stream opens the main call.
_PHASE_RE = re.compile(r"\bmessage=stream\b")
#: Log lines carry a wall clock and a run id that change on every line, so a
#: repeated fatal line never compares equal until both are removed.
_LOG_TS_RE = re.compile(r"\btimestamp=\S+")
_LOG_RUN_RE = re.compile(r"\brun=\S+")


def is_cli_log(line):
    """True for the CLI's own log narration (the liveness signal)."""
    return bool(_LOG_RE.search(line))


def is_banner(line):
    """True for the non-TTY banner, ANSI stripped, BEFORE any strip() test."""
    return bool(_BANNER_RE.match(_ANSI_RE.sub("", line)))


def is_phase(line):
    """True when a log line says the MAIN model call has opened."""
    if "small=true" in line:
        return False
    return bool(_PHASE_RE.search(line))


def resolve_window(model=None, explicit=None):
    """The FIRST-OUTPUT window: an explicit number wins, else the table."""
    if explicit is not None:
        return int(explicit)
    if model and no_output_sec_for is not None:
        return int(no_output_sec_for(model))
    return DEFAULT_NO_OUTPUT_SEC


def resolve_inter_token_window(explicit=None):
    """The window used once assistant text has begun. 240s unless overridden."""
    if explicit is not None:
        return int(explicit)
    return int(INTER_TOKEN_SEC or DEFAULT_INTER_TOKEN_SEC)


def _normal_key(line):
    """The same fatal line, minus the timestamp and run id in front of it."""
    text = _LOG_TS_RE.sub("", _LOG_RUN_RE.sub("", line or ""))
    return " ".join(_TS_RE.sub("", text.lower()).split())


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
    """Drain the child's merged output: tee it, classify it, count repeats.

    Three kinds of line, and they are NOT interchangeable:

      * a CLI LOG line -- liveness. It advances the clock, and a MAIN-call
        stream line (`message=stream` without `small=true`) additionally says
        the model call is open, which switches to the inter-token window. Log
        lines are ALSO classified: with `--print-logs` the CLI wraps the
        decisive provider error inside a log line (`level=ERROR ... error.error=
        "AI_APICallError: Free usage limit reached"`), and treating log lines as
        liveness-only made every rejection invisible and cost the whole window.
      * the BANNER -- decoration. Neither text nor liveness.
      * anything else -- assistant TEXT. In non-TTY mode this only ever arrives
        once the whole message is complete, so it is recorded as COMPLETION and
        never used as liveness.
    """
    try:
        for raw in stream:
            line = _ANSI_RE.sub("", raw)
            sink.write(line)
            sink.flush()
            if is_banner(line):
                continue
            if is_cli_log(line):
                state["last"] = time.monotonic()
                if is_phase(line):
                    state["phase"] = True
            elif line.strip() and not (is_error_shaped is not None
                                       and is_error_shaped(line)):
                state["text"] = True
                state["completed"] = True
            if classify_stream_line is None:
                continue
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


def supervise(cmd, no_output_sec=None, sink=None, model=None,
              inter_token_sec=None):
    """Run `cmd`; 124 if it went silent, 125 if it was rejected or repeating."""
    out = sink or sys.stdout
    first = resolve_window(model=model, explicit=no_output_sec)
    inter = resolve_inter_token_window(explicit=inter_token_sec)
    if classify_stream_line is None:
        print("model_watchdog: classifier unavailable; supervision is "
              "silence-only (first_output=%ss inter_token=%ss)"
              % (first, inter), file=sys.stderr)
    proc = subprocess.Popen(
        list(cmd), stdin=sys.stdin, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, errors="replace",
        bufsize=1, encoding="utf-8")
    state = {"last": time.monotonic(), "fatal": None, "repeat": None,
             "text": False, "phase": False, "completed": False, "outage": {}}
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
            # TWO windows, one clock, and the clock is the CLI's LOG. Before
            # the model call opens the attempt is judged on startup+dispatch;
            # afterwards a quiet log is a long generation, not a dead stream.
            # A COMPLETED burst is recorded but deliberately does NOT stop the
            # clock: stdout proves the message arrived, not that the process
            # will exit, and an unbounded wait is the failure this file exists
            # to prevent. The caller's `timeout` is the outer bound either way.
            window = inter if state["phase"] else first
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
    explicit, inter_explicit, model, cmd = None, None, None, []
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
        elif arg == "--inter-token-sec" and i + 1 < len(argv):
            try:
                inter_explicit = int(argv[i + 1])
            except ValueError:
                print("model_watchdog: --inter-token-sec must be an integer",
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
                  "--model ID | --no-output-sec N | --inter-token-sec N "
                  "-- CMD...)" % arg, file=sys.stderr)
            return 2
    if not cmd:
        print("model_watchdog: no command after --", file=sys.stderr)
        return 2
    first = resolve_window(model=model, explicit=explicit)
    inter = resolve_inter_token_window(explicit=inter_explicit)
    # Stated once, in the attempt log, so the windows that were enforced are
    # auditable afterwards instead of being inferred from the file's constants.
    print("model_watchdog: first_output=%ss inter_token=%ss model=%s"
          % (first, inter, model or "?"), file=sys.stderr)
    return supervise(cmd, no_output_sec=first, model=model,
                     inter_token_sec=inter)


if __name__ == "__main__":
    sys.exit(main())

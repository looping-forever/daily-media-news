#!/usr/bin/env python3
"""scripts/provider_canary.py -- the weekly alarm for a family that went dark.

WHY THIS EXISTS. Every other guard protects the EDITION: a card cannot ship
blank, the repair names its own refusal, the ladder drops a rung a catalogue no
longer serves. None of them tells the OWNER that a provider retired a model, or
a whole service. Probe 42363318770 is the case: GitHub Models answered HTTP 200
with the plain text `OK` to every path, the run shipped, and the only way to
learn it was to read a log by hand.

WHAT IT IS NOT. Not the manual probe. `provider-probe.yml` is the diagnostic the
owner runs to see every row; this is the alarm that fails the run and names the
fix. Both call `llm_providers.probe()`, so a canary that disagrees with the
probe has a bug -- it does not get a second opinion.

TWO EXCUSED STATES, SAID OUT LOUD. `quota` is capacity and clears by itself;
`not_configured` is a repository setting. A canary that cried wolf on either
would be ignored inside a month, which is worse than no canary.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import llm_providers as lp  # noqa: E402

#: Every family this repository can walk. The second Gemini project is separate
#: because it is a second account with its own allowance, and its absence is a
#: setting rather than a fault.
FAMILIES = ("gemini", lp.GEMINI_KEY2_PROVIDER) + lp.LADDER_PROVIDERS

#: Verdicts that are NOT a failure, with the reason printed beside the row.
EXCUSED = {
    "quota": "rate-limited; capacity clears by itself",
    "not_configured": "its secret is not set; a repository setting, not a fault",
}

#: One retry for a transport-shaped verdict: a weekly alarm must not fire on a
#: blip, and a family that is really gone fails the second ask too.
RETRIED = ("network", "timeout")


def advice(name, row):
    """One imperative line saying what to DO about a dark family."""
    record = lp.PROVIDERS[name]
    verdict = row["verdict"]
    if verdict == "auth":
        return ("check the scope and the account behind %s -- the secret is "
                "PRESENT and was refused; then refresh the model id %r from "
                "provider-probe.yml" % (record.env, record.models[0]))
    if verdict == "model_not_found":
        return ("refresh the model id %r from provider-probe.yml's `available` "
                "line" % (record.models[0],))
    if verdict == "blocked":
        return ("the request never reached %s -- an IP/egress policy or this "
                "client's shape, not the secret" % record.env)
    return ("run provider-probe.yml and read its note; if a manual request also "
            "lists nothing, %s has retired or moved the service -- its endpoint "
            "can still answer 200 with a non-API body, so a status check alone "
            "cannot see it. Refresh the id %r, or drop the family"
            % (name, record.models[0]))


def check(environ=None, transport=None, sleep=None):
    """(lines, dark) -- one line per family in FAMILIES order, dark ones too."""
    lines = []
    dark = []
    for name in FAMILIES:
        row = lp.probe(name, transport=transport, environ=environ, sleep=sleep)
        if row["verdict"] in RETRIED:
            row = lp.probe(name, transport=transport, environ=environ,
                           sleep=sleep)
        verdict = row["verdict"]
        if verdict == "ok":
            lines.append("  %-7s %-11s models=%-2d %s"
                         % ("ok", name, row["models"], row["chosen"] or "-"))
        elif verdict in EXCUSED:
            lines.append("  %-7s %-11s %s -- %s"
                         % ("excused", name, verdict, EXCUSED[verdict]))
        else:
            line = "DARK %s (%s): %s" % (name, verdict, advice(name, row))
            lines.append("  " + line)
            dark.append(line)
    return lines, dark


def main(environ=None, transport=None, sleep=None):
    """Print the table, then the verdict. Returns the process exit code."""
    lines, dark = check(environ=environ, transport=transport, sleep=sleep)
    for line in lines:
        print(line)
    excused = len([line for line in lines if line.startswith("  excused")])
    print("canary: %d families checked, %d fine (%d excused), %d DARK"
          % (len(lines), len(lines) - len(dark), excused, len(dark)))
    if dark:
        print("canary: the ladder keeps trying these and the edition keeps "
              "degrading quietly -- this alarm exists so that is not how you "
              "find out")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

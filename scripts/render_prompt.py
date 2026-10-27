"""Print a committed prompt with its {{TOKEN}} placeholders resolved.

    python3 scripts/render_prompt.py prompts/daily-digest.md

Stdout is the prompt the model receives. Diagnostics go to stderr so they can
never end up inside the prompt itself. Exit status is always 0: a prompt with
an unresolved token still has to run, and `private_values` has already warned.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import private_values  # noqa: E402  (deliberate: after the sys.path insert)


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: render_prompt.py PROMPT_PATH\n")
        return 2
    # The prompts carry arrows and em dashes. Piping on Windows defaults stdout
    # to the console code page (cp1252), which cannot encode them and kills the
    # step with UnicodeEncodeError instead of printing the prompt.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with open(argv[1], "r", encoding="utf-8") as handle:
        text = handle.read()
    sys.stdout.write(private_values.substitute_tokens(text))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

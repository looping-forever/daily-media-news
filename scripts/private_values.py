"""The one place the deployment's private proper nouns are read.

The bank, the college and their URLs used to be string literals in four
modules, which meant a public repository carried a committed copy of values
that only one deployment ever wanted. They are runtime values now: read from
the environment, with the old literal kept as a LAST-RESORT default.

NOTHING HERE IS SECRET. The values are private in the sense of "belongs to one
owner, not to everyone who clones the repo", so they live in the same place the
other deployment-specific values live (an env var fed by a GitHub secret) and
never in the tree.

Loud, once, when a default is in force: a digest that silently renders
"Example College" reaches the owner's phone looking correct and is not. The
message names the environment variable and never a value.
"""

import os
import sys

BANK_NAME_ENV = "BANK_NAME"
COLLEGE_NAME_ENV = "COLLEGE_NAME"
BANK_URL_ENV = "BANK_URL"
COLLEGE_URL_ENV = "COLLEGE_URL"

BANK_NAME_PLACEHOLDER = "MY BANK \u2014 EXAMPLE BANK"
COLLEGE_NAME_PLACEHOLDER = "Example College / COLLEGE"
BANK_URL_PLACEHOLDER = "https://www.bank.example.com/"
COLLEGE_URL_PLACEHOLDER = "https://college.example.edu/"

_warned = set()


def _value(env_name, placeholder, what):
    """Read env_name, else the placeholder, warning once by NAME only."""
    raw = os.environ.get(env_name)
    value = (raw or "").strip()
    if value:
        return value
    if env_name not in _warned:
        _warned.add(env_name)
        print(
            "PRIVATE-CONFIG: " + env_name + " is not set, so the placeholder "
            "for the " + what + " is in use and will appear in the output. "
            "Set " + env_name + " to the deployment's own value.",
            file=sys.stderr,
        )
    return placeholder


def bank_name():
    return _value(BANK_NAME_ENV, BANK_NAME_PLACEHOLDER, "bank's display name")


def college_name():
    return _value(COLLEGE_NAME_ENV, COLLEGE_NAME_PLACEHOLDER, "college's display name")


def bank_url():
    return _value(BANK_URL_ENV, BANK_URL_PLACEHOLDER, "bank notice page")


def college_url():
    return _value(COLLEGE_URL_ENV, COLLEGE_URL_PLACEHOLDER, "college notice page")


def _host(url):
    from urllib.parse import urlparse
    try:
        return urlparse(url).netloc.lower()
    except ValueError:
        return ""


def bank_host():
    return _host(bank_url())


def college_host():
    return _host(college_url())


# ---------------------------------------------------------------------------
# Prompt tokens. The two AI prompts are committed text, so the proper nouns
# reach the model as {{TOKENS}} resolved at pipe time. An unresolved token is
# left visible and warned about, because a prompt that quietly reads "None"
# classifies relevance against nothing.
# ---------------------------------------------------------------------------
TOKENS = {
    "BANK_NAME": bank_name,
    "COLLEGE_NAME": college_name,
    "BANK_URL": bank_url,
    "COLLEGE_URL": college_url,
}


def substitute_tokens(text):
    """Replace {{TOKEN}} in *text* with the runtime value of each token."""
    out = text
    for name, getter in TOKENS.items():
        token = "{{" + name + "}}"
        if token in out:
            out = out.replace(token, getter())
    return out

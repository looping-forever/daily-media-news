"""The one place the deployment's private proper nouns are read.

The bank, the college and their URLs used to be string literals in four
modules, which meant a public repository carried a committed copy of values
that only one deployment ever wanted. They are runtime values now: read from
the environment.

NOTHING HERE IS SECRET. The values are private in the sense of "belongs to one
owner, not to everyone who clones the repo", so they live in the same place the
other deployment-specific values live (an env var fed by a GitHub secret) and
never in the tree.

AN UNSET VALUE IS NOT REACHABLE, AND SAYS SO
--------------------------------------------
The display names still fall back to the sanitised stand-ins the public tree
uses ("MY BANK -- EXAMPLE BANK"), because a section heading is a label rather
than an address and the page has to render something. The URLs do NOT have such
a fallback: a placeholder host is a link to a site that does not exist, and the
collector would make a real request to it. Unset, they are the empty string, so
nothing is rendered and nothing is fetched.

Loud, once, naming the variable and never a value: a digest that silently
renders "Example College" reaches the owner's phone looking correct and is not.
That is what the PRIVATE-CONFIG line is for.
"""

import os
import sys

BANK_NAME_ENV = "BANK_NAME"
COLLEGE_NAME_ENV = "COLLEGE_NAME"
BANK_URL_ENV = "BANK_URL"
COLLEGE_URL_ENV = "COLLEGE_URL"

BANK_NAME_PLACEHOLDER = "MY BANK \u2014 EXAMPLE BANK"
COLLEGE_NAME_PLACEHOLDER = "Example College / COLLEGE"

#: No URL placeholder on purpose. A committed example host gets fetched by the
#: collector and rendered as a link to a site that has never existed; the empty
#: string is the only honest default, and it is filtered out by every consumer.
BANK_URL_UNSET = ""
COLLEGE_URL_UNSET = ""

_warned = set()


def _value(env_name, default, what):
    """Read env_name, else `default`, warning once by NAME only."""
    raw = os.environ.get(env_name)
    value = (raw or "").strip()
    if value:
        return value
    if env_name not in _warned:
        _warned.add(env_name)
        if default:
            consequence = ("the placeholder for the " + what +
                           " is in use and will appear in the output")
        else:
            consequence = ("nothing is rendered for the " + what +
                           " and no request is made for it")
        print(
            "PRIVATE-CONFIG: " + env_name + " is not configured, so " +
            consequence + ". Set " + env_name + " to the deployment's own value.",
            file=sys.stderr,
        )
    return default


def bank_name():
    return _value(BANK_NAME_ENV, BANK_NAME_PLACEHOLDER, "bank's display name")


def college_name():
    return _value(COLLEGE_NAME_ENV, COLLEGE_NAME_PLACEHOLDER, "college's display name")


def bank_url():
    return _value(BANK_URL_ENV, BANK_URL_UNSET, "bank notice page")


def college_url():
    return _value(COLLEGE_URL_ENV, COLLEGE_URL_UNSET, "college notice page")


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
# classifies relevance against nothing. An EMPTY value is treated the same way:
# substituting "" would delete the token and leave a sentence with a hole in it.
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
            value = getter()
            if value:
                out = out.replace(token, value)
    return out

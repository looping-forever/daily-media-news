#!/usr/bin/env python3
"""archive_origin.py -- resolve the digest archive hostnames the privacy canary probes.

WHY THIS FILE EXISTS
--------------------
`drift-canary.yml`'s "Digest archive is not publicly readable (origin auth
canary)" step used to carry its two probe hostnames as a literal:

    HOSTS="daily-digest.example.pages.dev master.daily-digest.example.pages.dev"

That literal is not the archive. `example` is reserved (RFC 2606) and Cloudflare
answers 404 for it, so the canary compared 404 against "expected 401/403" and
went red on every scheduled run from the day the git blame last touched that
line -- measuring a hostname that hosts nothing, while the real origin was never
probed once.

It could not simply be corrected. `scripts/check_public_sanitisation.py` runs on
every commit and the private sanitiser rewrites the real Pages hostname in every
published file, so a hostname written here comes back as the placeholder. The
value therefore has to be RESOLVED at run time from the repository variable
`CF_ARCHIVE_ORIGIN` -- the Actions-side twin of the Worker's
`DIGEST_ARCHIVE_ORIGIN` secret. One value, two stores, because a public
repository must not carry the hostname at all.

WHY A PLACEHOLDER IS A HARD FAILURE, NOT A PROBE
------------------------------------------------
A 404 from a hostname Cloudflare hands out to anyone is not a statement about
this archive, and a guard that reports "expected 401/403" forever teaches the
owner to ignore it. The one thing this canary exists to notice -- a deploy that
lost `functions/_middleware.js` and republished the whole archive unprotected --
is exactly the thing a red-every-night canary stops being looked at for. So an
unset or placeholder origin exits non-zero with a `::error::` line, and the step
aborts instead of reporting a verdict it did not measure.

USAGE
-----
    python scripts/archive_origin.py            # print the resolved origin
    python scripts/archive_origin.py --hosts    # print probe hostnames, one per line

Exit codes: 0 resolved, 2 the origin is unset or unusable (the message says why).
"""

import argparse
import os
import sys
from urllib.parse import urlsplit

#: Read in this order. The Worker's own name first (the same value it holds as a
#: secret), then the Actions repository variable, which is where CI can supply it
#: without the hostname being committed.
ENV_NAMES = ("DIGEST_ARCHIVE_ORIGIN", "CF_ARCHIVE_ORIGIN")

#: Hostname labels that mean "no value was configured". `example` and `invalid`
#: are reserved for exactly this, `test` is reserved by RFC 6761 too, and
#: `localhost` is not a public archive. Probing any of them measures a 404.
PLACEHOLDER_LABELS = frozenset({"example", "invalid", "test", "localhost"})


class ArchiveOriginError(Exception):
    """The configured origin is absent, malformed, or a placeholder."""


def configured_origin(environ=None):
    """Return (variable_name, raw_value) for the first configured origin.

    The name is returned even when nothing is set, so a refusal can tell the
    reader which variable it looked for. An empty value counts as unset: an
    unset repository variable reaches a workflow step as an empty string, and
    treating that as "configured" would probe `https:///`.
    """
    environ = os.environ if environ is None else environ
    for name in ENV_NAMES:
        value = (environ.get(name) or "").strip()
        if value:
            return name, value
    return ENV_NAMES[0], ""


def resolve_origin(raw, name=ENV_NAMES[0]):
    """Normalise one origin string to `https://host`, or raise ArchiveOriginError."""
    value = (raw or "").strip()
    if not value:
        raise ArchiveOriginError(
            "%s is not set, so the archive origin is unknown. Set the Actions "
            "repository variable CF_ARCHIVE_ORIGIN (the same value the Worker "
            "holds as DIGEST_ARCHIVE_ORIGIN) to the Pages hostname of the "
            "archive. Without it this probe would measure a hostname nobody "
            "configured, and a placeholder answers 404 forever." % name)

    parts = urlsplit(value)
    if parts.scheme != "https":
        raise ArchiveOriginError(
            "%s is %r, which is not an https origin. The canary measures an "
            "authentication gate, so it must reach the origin the way a browser "
            "does." % (name, value))
    if not parts.hostname:
        raise ArchiveOriginError(
            "%s is %r, which carries no hostname." % (name, value))
    if parts.username or parts.password:
        raise ArchiveOriginError(
            "%s is %r, which carries credentials. The canary deliberately sends "
            "none -- an anonymous probe is the measurement." % (name, value))
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ArchiveOriginError(
            "%s is %r, which is a URL with a path, query or fragment. The "
            "canary probes origin roots, one hop per hostname, so it needs a "
            "bare origin." % (name, value))

    # `parts.hostname` is lowercased and strips any port, which is what the
    # matcher wants. A Pages origin has no port, so a port in `netloc` means the
    # variable is holding something else.
    if ":" in parts.netloc:
        raise ArchiveOriginError(
            "%s is %r, which carries a port. A Pages origin does not." % (name, value))

    host = parts.hostname
    labels = host.split(".")
    blocked = [label for label in labels if label in PLACEHOLDER_LABELS]
    if blocked:
        raise ArchiveOriginError(
            "%s is %r, which is a PLACEHOLDER hostname (%r is reserved). "
            "Cloudflare answers a 404 for it, so probing it would report "
            "'expected 401/403' on every run while checking nothing at all -- "
            "which is the defect this resolver replaced. Set the real Pages "
            "hostname in the repository variable." % (name, value, blocked[0]))

    return "https://" + host


def probe_hosts(origin):
    """The hostnames this origin is reachable at, as a tuple.

    Pages serves one project from two public hostnames: the `<project>.pages.dev`
    production one and `<branch>.<project>.pages.dev`. Both are probed, because a
    guard that checks only the first leaves the second as an unmonitored way in
    -- and the second is exactly what notices the day the Cloudflare Access
    application in front of it is removed.
    """
    host = urlsplit(origin).netloc.lower()
    hosts = [host]
    if host.endswith(".pages.dev"):
        hosts.append("master." + host)
    return tuple(hosts)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hosts", action="store_true",
                        help="print the probe hostnames instead of the origin")
    args = parser.parse_args(argv)

    name, raw = configured_origin()
    try:
        origin = resolve_origin(raw, name)
    except ArchiveOriginError as error:
        # A GitHub annotation, so the refusal is visible on the run summary and
        # not only buried in the step log.
        print("::error::%s" % error)
        return 2

    if args.hosts:
        for host in probe_hosts(origin):
            print(host)
    else:
        print(origin)
    return 0


if __name__ == "__main__":
    sys.exit(main())

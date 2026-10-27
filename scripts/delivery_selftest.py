#!/usr/bin/env python3
"""delivery_selftest.py -- prove the two BACKUP channels reach the owner.

WHY THIS FILE EXISTS
--------------------
The digest's primary channel is ntfy. Gmail SMTP and the Telegram Bot API are
FALLBACKS, and both were only ever exercised as a side effect of something else
(a breaking alert, or a real digest). That means the two channels the owner
depends on when the primary is down were the two nobody could test on demand,
and the first real test would have been a real outage.

This runs the SAME transport code the production paths use -- it imports
`breaking_monitor` rather than re-implementing an HTTP call or an SMTP session
-- and sends EXACTLY ONE labelled message per channel:

    Telegram : "Test message from daily-media-news, ignore"
    Gmail    : subject "Test from daily-media-news, ignore"

There is NO digest content in either message. This is a channel test, not a
delivery test.

WHAT IT PRINTS, AND WHAT IT MUST NEVER PRINT
--------------------------------------------
Only presence, length, pass/fail and the evidence a human can check: the
Telegram `message_id` and `date`, the masked chat id, and the SMTP status code
plus message on failure. The bot token, the app password and the mail address
are NEVER printed -- every line passes through `_scrub()` first, so even an
exception that somehow quoted one of them cannot leak it into a public
workflow log. This repository is public; a secret in a log is a leaked secret.

WHY IT TRIES 465 AFTER 587
--------------------------
`smtp.gmail.com:587` with STARTTLS is the transport the repo actually uses
(`breaking_monitor.send_email`), so it is tried FIRST and its failure is the
one that matters. Port 465 with implicit SSL is a DIFFERENT failure mode --
egress blocked on 587, a network that strips STARTTLS, or a server that accepts
the connection and never speaks -- so it is tried ONCE as a diagnostic. If it
succeeds, the channel works and port 587 is the thing that is broken. If both
fail, both codes are reported. Production has no 465 fallback; that is a
deliberate gap recorded here rather than a silent one.

RUN IT
------
    python scripts/delivery_selftest.py

Locally that requires TELEGRAM_BOT_TOKEN / GMAIL_USER / GMAIL_APP_PASSWORD in
the environment. In CI it runs from `.github/workflows/delivery-selftest.yml`,
which supplies them from repository secrets. Exit code 0 = both channels
delivered; 1 = at least one failed (the workflow goes red, which is the point).

Nothing is written to disk. Nothing is retried: one message per channel per
run, because a channel test that sends twice proves less than one that sends
once and reports honestly.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import breaking_monitor as bm  # noqa: E402  (deliberate: after sys.path)

#: The exact bodies the brief specifies. Not templated, not parameterised: a
#: test message that changes shape between runs is not a control.
TELEGRAM_TEXT = "Test message from daily-media-news, ignore"
GMAIL_SUBJECT = "Test from daily-media-news, ignore"
GMAIL_TEXT = ("Test from daily-media-news, ignore\n\n"
              "No digest content. Sent by scripts/delivery_selftest.py.")

SMTP_HOST = "smtp.gmail.com"
SMTP_STARTTLS_PORT = 587
SMTP_SSL_PORT = 465

#: Every value that must never reach stdout. Filled in main() from the
#: environment; `_scrub` is a last line of defence, not the only one -- the
#: normal path prints lengths and masks, never values.
_SECRETS = []


def _scrub(text):
    """Replace any secret value inside *text* with a marker. Never raises."""
    out = str(text)
    for value in _SECRETS:
        if value and len(value) >= 4:
            out = out.replace(value, "<redacted>")
    return out


def _emit(text=""):
    print(_scrub(text))


def _env(name):
    return (os.environ.get(name) or "").strip()


def _mask(value):
    """First two characters then a marker -- enough to tell two ids apart,
    not enough to use one."""
    text = str(value)
    if len(text) <= 2:
        return "**"
    return text[:2] + "****"


def _app_password_shape(password):
    """(looks_like_app_password, length_without_spaces).

    A Google app password is 16 characters once the display spaces are
    stripped. An account password is not, and Gmail rejects it with 535. This
    distinguishes the two WITHOUT reading or printing the value, so a 535 can
    be attributed to the right cause instead of guessed at.
    """
    stripped = password.replace(" ", "")
    return (len(stripped) == 16 and stripped.isalnum(), len(stripped))


def _smtp_detail(exc):
    """(smtp_code|None, one-line message) for any exception smtplib raised."""
    code = getattr(exc, "smtp_code", None)
    detail = getattr(exc, "smtp_error", None)
    if detail is None:
        detail = str(exc)
    if isinstance(detail, bytes):
        detail = detail.decode("utf-8", "replace")
    detail = " ".join(str(detail).split())
    if not detail:
        detail = type(exc).__name__
    return code, detail[:400]


def _report_updates_shape(token):
    """Print the SHAPE of getUpdates when discovery found nothing usable.

    Called only on the failure path, and it prints update counts, chat TYPES
    and masked ids -- never the token, never a full chat id. Without this the
    owner's only evidence would be "no chat id", which does not say whether the
    queue is empty, holds a group, or holds only bot traffic.

    TWO FOLLOW-UP CALLS, because an empty queue has more than one cause and
    guessing between them wastes the owner's time:

    * `getMe` names the bot the TOKEN belongs to. The bot username is public
      (it is searchable in any Telegram client), so printing it is safe and it
      answers the question an empty queue cannot: did the owner message THIS
      bot, or a different one? "I sent /start" plus "no updates" is otherwise a
      contradiction with no way to resolve it from this side.
    * `getUpdates` with `offset=-1` returns the most recent update EVEN IF it
      has already been confirmed. If that yields a chat while the unconfirmed
      queue is empty, the update exists but something consumed it -- a
      materially different fault from "nothing ever arrived". The update's own
      `date` is printed so a >24h-old message (Telegram's retention) is
      distinguishable from a live one.
    """
    try:
        _status, body = bm.tg_http(
            "https://api.telegram.org/bot%s/getMe" % token, None)
        me = json.loads(body)
        if isinstance(me, dict) and me.get("ok") is True:
            result = me.get("result") or {}
            _emit("  getMe: ok=true username=@%s is_bot=%r"
                  % (result.get("username"), result.get("is_bot")))
        else:
            _emit("  getMe: ok=%r -- the token itself may be wrong"
                  % (me.get("ok") if isinstance(me, dict) else None))
    except Exception as exc:
        _emit("  getMe: transport error %s" % type(exc).__name__)

    try:
        status, body = bm.tg_http(
            "https://api.telegram.org/bot%s/getUpdates" % token,
            {"offset": -1, "limit": 1})
        data = json.loads(body)
    except Exception as exc:
        _emit("  getUpdates offset=-1: transport error %s" % type(exc).__name__)
        return
    if not isinstance(data, dict):
        _emit("  getUpdates offset=-1: non-JSON (HTTP %s)" % status)
        return
    results = data.get("result")
    if not isinstance(results, list) or not results:
        _emit("  getUpdates offset=-1: result_count=0 -- no update has EVER "
              "reached this bot, confirmed or not")
        return
    for upd in results:
        if not isinstance(upd, dict):
            continue
        message = upd.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        _emit("  getUpdates offset=-1: an update DOES exist -- "
              "chat.type=%r chat.id=%s date=%s (already confirmed, so the "
              "unconfirmed queue is empty)"
              % (chat.get("type"),
                 _mask(chat_id) if chat_id is not None else None,
                 message.get("date")))


def _report_updates_queue(token):
    """The plain, unconfirmed-queue shape -- the first thing printed on failure."""
    try:
        status, body = bm.tg_http(
            "https://api.telegram.org/bot%s/getUpdates" % token, None)
    except Exception as exc:
        _emit("  getUpdates: transport error %s" % type(exc).__name__)
        return
    try:
        data = json.loads(body)
    except ValueError:
        _emit("  getUpdates: non-JSON response (HTTP %s)" % status)
        return
    if not isinstance(data, dict):
        _emit("  getUpdates: unexpected top-level type %s" % type(data).__name__)
        return
    results = data.get("result")
    count = len(results) if isinstance(results, list) else "n/a"
    _emit("  getUpdates: HTTP %s ok=%r result_count=%s"
          % (status, data.get("ok"), count))
    for upd in (results or [])[:10]:
        if not isinstance(upd, dict):
            continue
        message = upd.get("message") or {}
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = chat.get("id")
        _emit("    update_id=%s chat.type=%r chat.id=%s from.is_bot=%r"
              % (upd.get("update_id"), chat.get("type"),
                 _mask(chat_id) if chat_id is not None else None,
                 sender.get("is_bot")))


def telegram_payload(chat_id, text):
    """The exact request body `breaking_monitor.tg_send` sends.

    Factored out so a test can compare it against tg_send's real payload: if
    production ever changes its request shape, this test stops being a test of
    production and the comparison fails instead of the divergence shipping
    quietly.
    """
    return {"chat_id": chat_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": False}


def telegram_test():
    """Send one labelled Telegram message. Returns True on a confirmed ok:true."""
    token = _env("TELEGRAM_BOT_TOKEN")
    if not token:
        _emit("telegram: FAIL  TELEGRAM_BOT_TOKEN absent -- channel not configured")
        return False

    explicit = _env("TELEGRAM_CHAT_ID")
    if explicit:
        chat_id = explicit
        source = "TELEGRAM_CHAT_ID secret"
    else:
        chat_id = bm.tg_get_chat_id(token)
        source = "auto-discovered from getUpdates"

    if chat_id is None:
        _emit("telegram: FAIL  no chat id -- getUpdates held no usable message.chat.id")
        _report_updates_queue(token)
        _report_updates_shape(token)
        _emit("  owner action: open a PRIVATE chat with the bot named above, send /start, re-run.")
        _emit("  durable fix: save the chat id as TELEGRAM_CHAT_ID (see the report).")
        return False

    _emit("telegram: chat id %s (%s)" % (_mask(chat_id), source))

    # The payload is byte-identical to tg_send's, so the test exercises the
    # production request shape; the difference is only that the RESPONSE body is
    # parsed here, because tg_send deliberately discards it and this script
    # exists to report message_id and date.
    payload = telegram_payload(chat_id, TELEGRAM_TEXT)
    try:
        status, body = bm.tg_http(
            "https://api.telegram.org/bot%s/sendMessage" % token, payload)
    except Exception as exc:
        _emit("telegram: FAIL  transport error %s: %s"
              % (type(exc).__name__, exc))
        return False
    try:
        data = json.loads(body)
    except ValueError:
        _emit("telegram: FAIL  non-JSON response (HTTP %s)" % status)
        return False
    if not isinstance(data, dict) or data.get("ok") is not True:
        description = ""
        if isinstance(data, dict):
            description = str(data.get("description") or "")
        _emit("telegram: FAIL  ok=false HTTP %s description=%r"
              % (status, description[:300]))
        return False

    result = data.get("result") or {}
    _emit("telegram: SENT  ok=true message_id=%s date=%s chat_id=%s"
          % (result.get("message_id"), result.get("date"), _mask(chat_id)))
    return True


def _send_gmail_ssl(user, password, receiver):
    """One send over implicit TLS (465). Diagnostic only -- see module docstring.

    `bm.send_email` calls starttls() unconditionally, so it cannot serve the
    465 path; this builds the same message and does the same login, on the other
    port. It is NOT a production transport and nothing imports it.
    """
    message = EmailMessage()
    message["Subject"] = GMAIL_SUBJECT
    message["From"] = user
    message["To"] = receiver
    message.set_content(GMAIL_TEXT)
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_SSL_PORT,
                          timeout=bm.SMTP_TIMEOUT_SEC, context=context) as session:
        session.login(user, password)
        session.send_message(message)


def gmail_test():
    """Send one labelled email. Returns True when a send was accepted."""
    user = _env("GMAIL_USER")
    password = _env("GMAIL_APP_PASSWORD")
    receiver = _env("GMAIL_RECEIVER")

    missing = [name for name, value in
               (("GMAIL_USER", user), ("GMAIL_APP_PASSWORD", password))
               if not value]
    if missing:
        _emit("gmail: FAIL  absent: %s -- channel not configured"
              % ", ".join(missing))
        return False

    looks_like_app_password, digits = _app_password_shape(password)
    _emit("gmail: GMAIL_USER present (len=%d)  GMAIL_APP_PASSWORD present "
          "(len=%d%s)  recipient=%s"
          % (len(user), digits,
             "" if looks_like_app_password
             else ", NOT 16 chars -- this may be an account password, not an app password",
             "GMAIL_RECEIVER" if receiver else "GMAIL_USER (fallback)"))

    html_body = "<p>%s</p>" % GMAIL_TEXT.replace("\n\n", "</p><p>").replace("\n", "<br>")
    try:
        bm.send_email(bm.default_smtp_factory, GMAIL_SUBJECT, html_body, GMAIL_TEXT)
        _emit("gmail: SENT  via %s:%d STARTTLS"
              % (SMTP_HOST, SMTP_STARTTLS_PORT))
        return True
    except Exception as exc:
        code, detail = _smtp_detail(exc)
        _emit("gmail: FAIL  port %d STARTTLS  %s%s"
              % (SMTP_STARTTLS_PORT,
                 ("SMTP %s " % code) if code else "",
                 detail))

    # Exactly one diagnostic attempt on the other port. Not a retry loop.
    try:
        _send_gmail_ssl(user, password, receiver or user)
        _emit("gmail: SENT  via %s:%d SSL  (port %d STARTTLS failed first)"
              % (SMTP_HOST, SMTP_SSL_PORT, SMTP_STARTTLS_PORT))
        return True
    except Exception as exc:
        code, detail = _smtp_detail(exc)
        _emit("gmail: FAIL  port %d SSL  %s%s"
              % (SMTP_SSL_PORT,
                 ("SMTP %s " % code) if code else "",
                 detail))
    return False


def main():
    _SECRETS[:] = [_env("TELEGRAM_BOT_TOKEN"),
                   _env("GMAIL_APP_PASSWORD"),
                   _env("GMAIL_USER")]
    # WHICH CHANNELS, AND WHY IT IS SELECTABLE. The contract is ONE labelled
    # message per channel per run. When one channel passes and the other fails,
    # re-running BOTH to retry the failure would send a second message on the
    # channel that already worked -- an extra unrequested email, and a weaker
    # test (the second send proves nothing the first did not). So a run may be
    # narrowed to the channel under investigation. "both" is the default and the
    # only value the normal path uses.
    selection = (_env("SELFTEST_CHANNELS") or "both").lower()
    if selection not in ("both", "telegram", "gmail"):
        _emit("SELFTEST_CHANNELS=%r is not one of both|telegram|gmail" % selection)
        return 2

    _emit("=== delivery self-test: one labelled message per channel, no digest content ===")
    _emit("channels: %s" % selection)

    telegram_ok = gmail_ok = None
    if selection in ("both", "telegram"):
        telegram_ok = telegram_test()
    if selection in ("both", "gmail"):
        gmail_ok = gmail_test()

    _emit("---")
    if telegram_ok is not None:
        _emit("telegram: %s" % ("PASS" if telegram_ok else "FAIL"))
    else:
        _emit("telegram: SKIPPED (not selected this run)")
    if gmail_ok is not None:
        _emit("gmail:    %s" % ("PASS" if gmail_ok else "FAIL"))
    else:
        _emit("gmail:    SKIPPED (not selected this run)")

    results = [r for r in (telegram_ok, gmail_ok) if r is not None]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

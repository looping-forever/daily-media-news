"""The delivery self-test's three channel states, EXECUTED, not inspected.

Run 40460125670 was red only because TELEGRAM_CHAT_ID was unset: a channel
that had never been configured reported FAIL, so red stopped meaning "a
configured channel is broken". These tests pin the contract that replaced it:

    configured + delivered -> PASS, exit 0
    configured + failed    -> FAIL, exit 1
    not configured         -> SKIPPED (not configured: <NAME>), exit 0

Nothing here touches the network: the production transports are replaced by
mocks, so every state is exercised through main() without sending a message.
"""

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import breaking_monitor as bm  # noqa: E402  (deliberate: after sys.path)
import delivery_selftest as ds  # noqa: E402

_UNSET = object()


def _send_ok(url, payload=None):
    return 200, '{"ok": true, "result": {"message_id": 7, "date": 1}}'


def _send_refused(url, payload=None):
    return 200, '{"ok": false, "description": "Forbidden: bot was blocked"}'


def _empty_updates(url, payload=None):
    """The diagnostics path: getMe names the bot, getUpdates holds nothing."""
    if "getMe" in url:
        return 200, ('{"ok": true, "result": '
                     '{"username": "testbot", "is_bot": true}}')
    return 200, '{"ok": true, "result": []}'


class DeliverySelftestChannelStateTests(unittest.TestCase):
    def setUp(self):
        bm._TG_CHAT_ID_CACHE.clear()
        self.addCleanup(bm._TG_CHAT_ID_CACHE.clear)

    def _run(self, env, tg_http=None, send_email=None, chat_id=_UNSET,
             send_gmail_ssl=None):
        """Run main() against a fully controlled environment.

        Every variable the script reads is set explicitly, so a developer's own
        TELEGRAM_BOT_TOKEN cannot turn the unconfigured cases into passes.
        """
        base = {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "",
                "GMAIL_USER": "", "GMAIL_APP_PASSWORD": "",
                "GMAIL_RECEIVER": "", "SELFTEST_CHANNELS": "telegram"}
        base.update(env)
        buf = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, base, clear=False))
            if tg_http is not None:
                stack.enter_context(mock.patch.object(bm, "tg_http", tg_http))
            if send_email is not None:
                stack.enter_context(
                    mock.patch.object(bm, "send_email", send_email))
            if send_gmail_ssl is not None:
                stack.enter_context(
                    mock.patch.object(ds, "_send_gmail_ssl", send_gmail_ssl))
            if chat_id is not _UNSET:
                stack.enter_context(
                    mock.patch.object(bm, "tg_get_chat_id",
                                      lambda token: chat_id))
            with contextlib.redirect_stdout(buf):
                code = ds.main()
        return code, buf.getvalue()

    # -- state 1: configured and delivered --------------------------------
    def test_configured_pass_is_green(self):
        code, out = self._run(
            {"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "42"},
            tg_http=_send_ok)
        self.assertEqual(0, code, out)
        self.assertIn("telegram: PASS", out)

    # -- state 2: configured and failed -----------------------------------
    def test_configured_fail_is_red(self):
        code, out = self._run(
            {"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "42"},
            tg_http=_send_refused)
        self.assertEqual(1, code,
                         "a channel that IS configured and fails must keep the "
                         "run red:\n%s" % out)
        self.assertIn("telegram: FAIL", out)

    def test_configured_gmail_fail_is_red(self):
        def boom(*args, **kwargs):
            raise RuntimeError("smtp refused")

        code, out = self._run(
            {"SELFTEST_CHANNELS": "gmail",
             "GMAIL_USER": "owner@example.com",
             "GMAIL_APP_PASSWORD": "abcdefghijklmnop"},
            send_email=boom, send_gmail_ssl=boom)
        self.assertEqual(1, code, out)
        self.assertIn("gmail:    FAIL", out)

    # -- state 3: not configured ------------------------------------------
    def test_unconfigured_bot_token_is_skipped_not_red(self):
        code, out = self._run({})
        self.assertEqual(0, code,
                         "an absent token is a missing destination, not a "
                         "broken transport:\n%s" % out)
        self.assertIn("telegram: SKIPPED (not configured: TELEGRAM_BOT_TOKEN)",
                      out)

    def test_unconfigured_chat_id_is_skipped_but_still_recorded(self):
        """The live case: the bot token exists, no chat id is configured, and
        getUpdates holds no usable chat. The verdict is SKIPPED, and the
        diagnostics are still printed so the state is RECORDED, not dropped."""
        code, out = self._run(
            {"TELEGRAM_BOT_TOKEN": "T"}, tg_http=_empty_updates, chat_id=None)
        self.assertEqual(0, code, out)
        self.assertIn("telegram: SKIPPED (not configured: TELEGRAM_CHAT_ID)",
                      out)
        self.assertIn("getUpdates", out,
                      "the diagnostics must still run: a skipped channel is "
                      "recorded, never silently dropped")

    def test_unconfigured_gmail_is_skipped_not_red(self):
        code, out = self._run({"SELFTEST_CHANNELS": "gmail"})
        self.assertEqual(0, code, out)
        self.assertIn("gmail:    SKIPPED (not configured: GMAIL_USER, "
                      "GMAIL_APP_PASSWORD)", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)

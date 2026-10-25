#!/usr/bin/env python3
"""render_academic.py — Render a TU B.Sc.CSIT result notification as HTML.

Reads a notification payload (as emitted by scripts/check_academic_result.py
into state/academic_notify_preview.json, or a single payload dict) and renders
a clean, self-contained HTML page (inline CSS, matching the digest site's dark
aesthetic) into docs/academic/.

The payload's ``website.path`` field (e.g. "docs/academic/2026-09-03-sem4.html")
determines the output file; ``website.url`` is the public URL shown on the page.

CLI:
  py scripts/render_academic.py --payload state/academic_notify_preview.json \
      --out-dir docs/academic

Stdlib only. Exit 0 on success, 1 on error.
"""

import argparse
import html
import json
import os
import sys

# The payload parse contract lives in exactly one place (scripts/payload_io.py)
# and this renderer is one of its consumers: it must NOT keep its own
# json.load, or the "four payloads fused into one unparseable file" hole
# reopens here. The script's own directory is put on sys.path so the import
# works both as `python3 scripts/render_academic.py` and when the workflow
# imports this module in-process.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import payload_io  # noqa: E402  (deliberate: after the sys.path insert)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PAYLOAD = os.path.join(REPO_ROOT, "state", "academic_notify_preview.json")
DEFAULT_OUT_DIR = os.path.join(REPO_ROOT, "docs", "academic")

# Digest-site aesthetic (see docs/index.html / docs/digests/*.html).
PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <meta name="payload-count" content="{payload_count}">
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
      background: #1a1a2e;
      color: #eee;
      line-height: 1.6;
      min-height: 100vh;
    }}
    .container {{ max-width: 720px; margin: 0 auto; padding: 2rem 1rem; }}
    .back-link {{
      display: inline-block;
      color: #00d4ff;
      text-decoration: none;
      font-size: 0.95rem;
      padding: 0.75rem 0.5rem 0.75rem 0;
      margin-bottom: 0.5rem;
    }}
    .back-link:hover {{ text-decoration: underline; }}
    header {{
      text-align: center;
      padding: 2rem 0;
      border-bottom: 1px solid #2a2a4a;
      margin-bottom: 2rem;
    }}
    h1 {{
      font-size: clamp(1.5rem, 4vw, 2rem);
      font-weight: 600;
      color: #fff;
      margin-bottom: 0.5rem;
    }}
    .subtitle {{ color: #00d4ff; font-size: clamp(0.9rem, 2.5vw, 1.1rem); font-weight: 400; }}
    .result-card {{
      background: #22223a;
      border: 1px solid #2a2a4a;
      border-radius: 10px;
      padding: 1.75rem;
      margin-bottom: 1.5rem;
    }}
    .badge {{
      display: inline-block;
      border-radius: 9999px;
      padding: 0.35rem 1rem;
      font-size: 1rem;
      font-weight: 700;
      letter-spacing: 0.04em;
      margin-bottom: 1.25rem;
    }}
    .badge.passed {{ background: #1f8a4c; color: #fff; }}
    .badge.failed {{ background: #b3261e; color: #fff; }}
    .field {{ margin-bottom: 1rem; }}
    .field-label {{
      font-size: 0.78rem;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: #00d4ff;
      margin-bottom: 0.2rem;
    }}
    .field-value {{ font-size: 1.05rem; color: #fff; }}
    .field-value a {{ color: #00d4ff; text-decoration: none; word-break: break-all; }}
    .field-value a:hover {{ text-decoration: underline; }}
    .note {{
      margin-top: 1.5rem;
      padding: 1rem 1.25rem;
      background: #1f2233;
      border-left: 4px solid #00d4ff;
      border-radius: 4px;
      font-size: 0.9rem;
      color: #ddd;
    }}
    .btn {{
      display: inline-block;
      background: #00d4ff;
      color: #06283d;
      border-radius: 8px;
      padding: 0.7rem 1.4rem;
      font-weight: 700;
      text-decoration: none;
      margin-top: 0.5rem;
      transition: background 0.2s, transform 0.2s;
    }}
    .btn:hover {{ background: #33ddff; transform: translateY(-1px); }}
    footer {{
      text-align: center;
      padding: 2rem;
      color: #999;
      font-size: 0.85rem;
      border-top: 1px solid #2a2a4a;
      margin-top: 2rem;
    }}
    @media (max-width: 480px) {{
      .container {{ padding: 1rem; }}
      .result-card {{ padding: 1.25rem; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <a class="back-link" href="../index.html">&larr; Back to archive</a>
    <header>
      <h1>&#127891; Academic Result</h1>
      <p class="subtitle">TU B.Sc.CSIT &middot; Semester {semester}{results_suffix}</p>
    </header>
    <div class="result-card">
      <span class="badge {status}">{badge_text}</span>
      <div class="field">
        <div class="field-label">Semester</div>
        <div class="field-value">Semester {semester}</div>
      </div>
      <div class="field">
        <div class="field-label">Roll Number</div>
        <div class="field-value">{roll_number}</div>
      </div>
      <div class="field">
        <div class="field-label">Result Date</div>
        <div class="field-value">{result_date}</div>
      </div>
      <div class="field">
        <div class="field-label">Notice</div>
        <div class="field-value">{notice_title}</div>
      </div>
      <div class="field">
        <div class="field-label">Result PDF</div>
        <div class="field-value"><a href="{pdf_url}" target="_blank" rel="noopener">View result PDF</a></div>
      </div>
      <a class="btn" href="{pdf_url}" target="_blank" rel="noopener">&#128196; View Result PDF</a>
    </div>
    {note_html}
    <footer>
      Generated by Personal News Informer &middot; Academic Result Monitor
    </footer>
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# F4 exam-notice / degraded templates.
#
# Kept in SEPARATE templates on purpose: the result page above must stay
# byte-identical, so the new kinds can never perturb it. Neither template
# contains a pass/fail badge — an exam notice is a pointer, not a verdict.
# ---------------------------------------------------------------------------

_PAGE_SHELL_CSS = """    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
      background: #1a1a2e;
      color: #eee;
      line-height: 1.6;
      min-height: 100vh;
    }
    .container { max-width: 720px; margin: 0 auto; padding: 2rem 1rem; }
    .back-link {
      display: inline-block;
      color: #00d4ff;
      text-decoration: none;
      font-size: 0.95rem;
      padding: 0.75rem 0.5rem 0.75rem 0;
      margin-bottom: 0.5rem;
    }
    .back-link:hover { text-decoration: underline; }
    header {
      text-align: center;
      padding: 2rem 0;
      border-bottom: 1px solid #2a2a4a;
      margin-bottom: 2rem;
    }
    h1 { font-size: clamp(1.3rem, 3.5vw, 1.8rem); font-weight: 600; color: #fff; margin-bottom: 0.5rem; }
    .subtitle { color: #00d4ff; font-size: clamp(0.9rem, 2.5vw, 1.1rem); font-weight: 400; }
    .notice-card {
      background: #22223a;
      border: 1px solid #2a2a4a;
      border-left: 4px solid #00d4ff;
      border-radius: 10px;
      padding: 1.75rem;
      margin-bottom: 1.5rem;
    }
    .notice-card.warn { border-left-color: #ffb020; }
    .field { margin-bottom: 1rem; }
    .field-label {
      font-size: 0.78rem;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: #00d4ff;
      margin-bottom: 0.2rem;
    }
    .field-value { font-size: 1.05rem; color: #fff; }
    .field-value a { color: #00d4ff; text-decoration: none; word-break: break-all; }
    .field-value a:hover { text-decoration: underline; }
    .note {
      margin-top: 1.5rem;
      padding: 1rem 1.25rem;
      background: #1f2233;
      border-left: 4px solid #00d4ff;
      border-radius: 4px;
      font-size: 0.9rem;
      color: #ddd;
      white-space: pre-wrap;
    }
    .btn {
      display: inline-block;
      background: #00d4ff;
      color: #06283d;
      border-radius: 8px;
      padding: 0.7rem 1.4rem;
      font-weight: 700;
      text-decoration: none;
      margin-top: 0.5rem;
      transition: background 0.2s, transform 0.2s;
    }
    .btn:hover { background: #33ddff; transform: translateY(-1px); }
    footer {
      text-align: center;
      padding: 2rem;
      color: #999;
      font-size: 0.85rem;
      border-top: 1px solid #2a2a4a;
      margin-top: 2rem;
    }
    @media (max-width: 480px) {
      .container { padding: 1rem; }
      .notice-card { padding: 1.25rem; }
    }"""

EXAM_NOTICE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
__CSS__
  </style>
</head>
<body>
  <div class="container">
    <a class="back-link" href="../index.html">&larr; Back to archive</a>
    <header>
      <h1>&#128197; CSIT Exam Notice</h1>
      <p class="subtitle">TU B.Sc.CSIT &middot; {semester}</p>
    </header>
    <div class="notice-card">
      <div class="field">
        <div class="field-label">Notice</div>
        <div class="field-value">{notice_title}</div>
      </div>
      <div class="field">
        <div class="field-label">Published</div>
        <div class="field-value">{notice_date}</div>
      </div>
      <div class="field">
        <div class="field-label">Link</div>
        <div class="field-value"><a href="{notice_url}" target="_blank" rel="noopener">Open the notice</a></div>
      </div>
      <a class="btn" href="{notice_url}" target="_blank" rel="noopener">&#128203; Open Notice</a>
    </div>
    <div class="note">This page carries NO pass/fail verdict. It alerts you that a routine,
schedule, form, admission-card or deadline notice was published. Dates, venues and
instructions live in the notice itself &mdash; open it and read it.</div>
    <footer>
      Generated by Personal News Informer &middot; Academic Exam-Notice Monitor
    </footer>
  </div>
</body>
</html>
""".replace("__CSS__", _PAGE_SHELL_CSS)

DEGRADED_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
__CSS__
  </style>
</head>
<body>
  <div class="container">
    <a class="back-link" href="../index.html">&larr; Back to archive</a>
    <header>
      <h1>&#9888;&#65039; Academic Monitor Degraded</h1>
      <p class="subtitle">Checked {checked_at}</p>
    </header>
    <div class="notice-card warn">
      <div class="field">
        <div class="field-label">What happened</div>
        <div class="field-value">{title}</div>
      </div>
      <div class="note">{message}</div>
    </div>
    <footer>
      Generated by Personal News Informer &middot; Academic Result Monitor
    </footer>
  </div>
</body>
</html>
""".replace("__CSS__", _PAGE_SHELL_CSS)


# ---------------------------------------------------------------------------
# Payload loading
# ---------------------------------------------------------------------------


def load_payloads(path):
    """Every payload in `path`, in order. Delegates to the ONE parse contract.

    Kept as a module-level name because the workflow's heredoc and older
    callers refer to it, but the rules are now payload_io's and nobody may
    re-implement them: JSON-Lines (the live format), a JSON list (the dry-run
    preview), ``[]`` for an absent/empty file, and PayloadFormatError --
    never a silently skipped line -- for anything else.
    """
    return payload_io.read_payloads(path)


def _semester_label(semester):
    """'4' -> '4' (kept simple; the payload already carries the number)."""
    return str(semester)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fill(template, mapping):
    """Token fill (not str.format) so inline CSS braces need no doubling."""
    for key, value in mapping.items():
        template = template.replace("@@%s@@" % key, value)
    return template


def _exam_notice_filename(result_date, semester):
    """F4: <filename> with NO pass/fail verdict anywhere."""
    label = str(semester) if semester else "all"
    return "%s-exam-notice-sem%s.html" % (result_date or "notice", label)


def render_exam_notice(payload, out_dir):
    """F4 exam-notice page: routines, forms, admission cards, deadlines.

    Deliberately carries NO pass/fail verdict and no roll-number verdict — an
    exam-notice hit is a pointer to a notice, not a judgement about the
    student. Rendered from EXAM_NOTICE_TEMPLATE only, so the result page
    output stays byte-identical.
    """
    semester = payload.get("semester")
    title = payload.get("title") or ""
    notice_url = payload.get("notice_url") or ""
    notice_date = payload.get("result_date") or ""
    sem_label = str(semester) if semester else "all semesters"

    web_path = (payload.get("website") or {}).get("path") or ""
    filename = os.path.basename(web_path) if web_path else \
        _exam_notice_filename(notice_date, semester)

    page = _fill(EXAM_NOTICE_TEMPLATE, {
        "title": html.escape("CSIT exam notice: %s" % title),
        "semester": html.escape(sem_label),
        "notice_date": html.escape(str(notice_date)),
        "notice_title": html.escape(str(title)),
        "notice_url": html.escape(str(notice_url), quote=True),
    })

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, filename)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return out_path


def render_degraded(payload, out_dir):
    """Degraded-run page (acceptance criterion 6): a monitor failure must
    reach the owner as something other than silence."""
    message = payload.get("message") or ""
    notice_date = payload.get("result_date") or ""
    web_path = (payload.get("website") or {}).get("path") or ""
    filename = os.path.basename(web_path) if web_path else \
        "%s-degraded.html" % (notice_date or "run")

    page = _fill(DEGRADED_TEMPLATE, {
        "title": html.escape(str(payload.get("title")
                                or "Academic monitor degraded")),
        "checked_at": html.escape(str(notice_date)),
        "message": html.escape(str(message)),
    })
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, filename)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return out_path


def render_payload(payload, out_dir, payload_count=1):
    """Render a single payload dict to an HTML file under out_dir.

    ``kind`` selects the template. ``"result"`` (the default, and the only
    kind check_academic_result.py emits for a verdict) keeps the historical
    output BYTE-IDENTICAL for a single payload. ``"exam_deadline"`` and
    ``"degraded"`` render through their own templates and can never emit a
    pass/fail verdict.

    ``payload_count`` is how many payloads the run emitted. It is rendered
    into the page (meta tag + subtitle) so a four-payload run cannot
    present itself to the reader as one result: the honest number on the
    page is the number of results the owner is looking at.

    Returns the output file path. The output filename comes from the payload's
    ``website.path`` field (e.g. "docs/academic/2026-09-03-sem4.html"); the
    directory portion is replaced by out_dir. EVERY payload in the file is
    rendered, each to its own archived page, and the workflow deploys the
    staged directory ONCE -- N payloads are never N deploys."""
    kind = payload.get("kind") or "academic_result"
    if kind in ("exam_deadline", "exam_notice"):
        return render_exam_notice(payload, out_dir)
    if kind == "degraded":
        return render_degraded(payload, out_dir)

    semester = payload.get("semester")
    roll_number = payload.get("roll_number") or ""
    passed = bool(payload.get("passed"))
    status = payload.get("status") or ("passed" if passed else "failed")
    result_date = payload.get("result_date") or ""
    pdf_url = payload.get("pdf_url") or ""
    notice_title = payload.get("title") or ""

    badge_text = "PASSED" if passed else "FAILED"
    badge_class = "passed" if passed else "failed"

    if passed:
        note_html = (
            '<div class="note">&#127881; Congratulations! Your roll number was '
            "found in the result. This semester is complete.</div>"
        )
    else:
        note_html = (
            '<div class="note">&#128269; Your roll number was not found in this '
            "result. The monitor will keep watching for your back / re-exam "
            "result and notify you when it is published.</div>"
        )

    # Determine the output filename from the payload's website.path, falling
    # back to a date-semester name.
    web_path = payload.get("website") or {}
    rel_path = web_path.get("path") or ""
    if rel_path:
        filename = os.path.basename(rel_path)
    else:
        filename = "%s-sem%s.html" % (result_date or "result", semester)

    page_title = "CSIT Sem %s Result: %s" % (semester, badge_text)
    # A single-payload run renders exactly the historical subtitle; only a
    # MULTI-payload run says how many results the page carries, so the honest
    # number is never added noise to the common case.
    try:
        shown = int(payload_count)
    except (TypeError, ValueError):
        shown = 1
    results_suffix = "" if shown <= 1 else " &middot; %d results" % shown

    page = PAGE_TEMPLATE.format(
        title=html.escape(page_title),
        payload_count=max(shown, 1),
        results_suffix=results_suffix,
        semester=html.escape(_semester_label(semester)),
        status=html.escape(badge_class),
        badge_text=html.escape(badge_text),
        roll_number=html.escape(str(roll_number)),
        result_date=html.escape(str(result_date)),
        notice_title=html.escape(str(notice_title)),
        pdf_url=html.escape(str(pdf_url), quote=True),
        note_html=note_html,
    )

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, filename)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Render a TU B.Sc.CSIT result notification as HTML")
    parser.add_argument("--payload", default=DEFAULT_PAYLOAD,
                        help="path to the notification payload file "
                             "(JSON-Lines, or a JSON list for the dry-run "
                             "preview; default: state/academic_notify_preview.json)")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                        help="output directory (default: docs/academic)")
    parser.add_argument("--payload-count", type=int, default=None,
                        help="how many payloads this page is carrying; goes "
                             "into the HTML meta and the subtitle so a "
                             "multi-payload run cannot present itself as a "
                             "single result (default: the number rendered)")
    args = parser.parse_args(argv)

    try:
        payloads = load_payloads(args.payload)
    except FileNotFoundError:
        print("render_academic: payload file not found: %s" % args.payload,
              file=sys.stderr)
        return 1
    except payload_io.PayloadFormatError as exc:
        # LOUD on purpose. The old code caught (OSError, ValueError) here and
        # the workflow's `||` chain then rendered the degraded page, so a
        # broken result payload was replaced by a "monitor degraded" page and
        # the run stayed green. A payload file that cannot be read is a
        # delivery failure, and delivery_assert turns it red.
        print("render_academic: %s" % exc, file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print("render_academic: cannot read payload %s: %s"
              % (args.payload, exc), file=sys.stderr)
        return 1

    if not payloads:
        print("render_academic: no payloads to render", file=sys.stderr)
        return 1

    count = args.payload_count if args.payload_count else len(payloads)
    if count < 1:
        count = len(payloads)

    written = []
    for payload in payloads:
        try:
            out_path = render_payload(payload, args.out_dir, count)
            written.append(out_path)
            print("render_academic: wrote %s" % out_path)
        except Exception as exc:
            print("render_academic: failed to render payload: %s" % exc,
                  file=sys.stderr)
            return 1

    print("render_academic: rendered %d page(s) carrying %d payload(s)"
          % (len(written), count))
    return 0


if __name__ == "__main__":
    sys.exit(main())

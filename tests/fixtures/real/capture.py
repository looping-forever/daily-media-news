"""Captures real bytes from the live sites into tests/fixtures/real/.

RUN BY HAND, deliberately. This is not a test and is never run by CI: a test
that reaches the internet is a test that fails on someone else's bad morning,
and this repository's whole lesson is that unverified runs are how a result gets
burned. Capture is a human act, reviewed, committed.

    python.exe tests/fixtures/real/capture.py

WHAT IS AND IS NOT COMMITTED
----------------------------
  * notice lists, notice detail pages and ONE feed: real bytes, verbatim, with
    a `.meta.json` sidecar recording provenance and a sha256.
  * a result PDF: NEVER. A result sheet is a table of other students' names,
    roll numbers and marks. "It is public" is not a licence to mirror that into
    a git repository, so what is committed is `pdf_shape_*.json` METADATA only:
    the source sha256, page count, whether roll-shaped tokens were present,
    split-or-contiguous, and which extractor read it. No student data, no text.

`tests/academic/real_pdf_builder.py` (not owned by this script) already builds
a structurally real one-page PDF, so PDF coverage is not duplicated here.

LINE ENDINGS ARE NORMALISED, AND THAT IS A CORRECTNESS RULE HERE
--------------------------------------------------------------
This tree is byte-hashed and the hashes are checked on Ubuntu CI, where git
applies no line-ending conversion. On a Windows checkout `core.autocrlf=true`
converts LF to CRLF on the way out, so a CRLF capture is committed as LF and the
sha256 recorded at capture time stops describing the committed bytes. That
happened for real on 2026-09-30: two captures were recorded as 742438 and 725
bytes, were committed as 742436 and 713, and `test_manifest_freshness` went red
on every CI run while staying green on the capturing machine. Every write below
therefore goes through `_lf()` and an explicit `newline="\n"` handle.
"""

import datetime
import hashlib
import io
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
for _p in (REPO_ROOT, os.path.join(REPO_ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import check_academic_result as AR        # noqa: E402
import collect_news as CN                  # noqa: E402

RESULT_LIST_URL = "https://iost.tu.edu.np/notices?notice_type=21"
UNION_LIST_URL = "https://iost.tu.edu.np/notices"

#: Detail pages worth capturing. 15673 is the real sem-1 2082 CSIT result (the
#: one the burn was about); 15696 is a genuine Devanagari-titled notice; 15642
#: is filed under a DIFFERENT notice_type and is only on the union list, which
#: is why the union exists.
DETAIL_IDS = (15673, 15696, 15642)

#: ONE real feed: a genuine Google News RSS search response, from the same
#: np-en sweep the digest runs, so D1/D2/D3 are proven against the exact body
#: the product's own collector asks for.
FEED_SPEC = ("np-en", "Nepal")

#: The notice whose PDF shape is recorded as metadata. It is the real sheet.
PDF_SHAPE_NOTICE_ID = 15673


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _lf(data):
    """Normalise CRLF to LF before a capture is written or hashed.

    WHY THIS EXISTS. This tree is byte-hashed, and the hashes are checked on
    Ubuntu CI, where git checks the files out with NO line-ending conversion.
    On a Windows checkout git's `core.autocrlf=true` converts LF to CRLF on the
    way out, so a capture written with CRLF is committed as LF, and the sha256
    recorded at capture time stops describing the committed bytes. That is not
    hypothetical: it happened on 2026-09-30 to two captures, and the manifest
    guard went red on every CI run while staying green on the machine that
    captured them -- a fixture that is provably intact being called a liar.

    HTML and RSS are line-ending agnostic, so normalising loses nothing: the
    parser reads the same document. It makes the recorded hash describe the same
    bytes on every platform, which is the only property the guard needs.

    Applied to captures AND to the JSON this script writes, for the same reason:
    `json.dump` under a text-mode Windows handle emitted CRLF for the same reason.
    """
    return data.replace(b"\r\n", b"\n")


def write_sample(origin, kind, date, ext, url, data, note, expect, guard,
                 out_dir, discriminator=""):
    """Write one capture plus its `.meta.json` sidecar, return the manifest row.

    NAMING: ``<kind>_<host>_<YYYY-MM-DD>.<ext>``. A capture that is not unique
    per (kind, host, day) -- three detail pages from one board, two feeds from
    one origin -- carries a ``.<discriminator>`` suffix inside the `<host>` slot
    so the day cannot overwrite itself. That is a real bug this script had: the
    second feed and the third detail page silently replaced their predecessors,
    and a fixture tree that quietly keeps only the last write is worse than no
    fixture at all, because the manifest then indexes bytes nobody captured.
    """
    host_slot = origin if not discriminator else "%s.%s" % (origin, discriminator)
    base = "%s_%s_%s%s" % (kind, host_slot, date, ext)
    path = os.path.join(out_dir, base)
    data = _lf(data)
    with open(path, "wb") as fh:
        fh.write(data)
    meta = {
        "source_url": url,
        "captured_at": _now(),
        "sha256": _sha256(data),
        "bytes": len(data),
        "trimmed": False,
        "note": note,
    }
    with io.open(path + ".meta.json", "w", encoding="utf-8", newline="\n") as fh:
        json.dump(meta, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    return {
        "file": base,
        "kind": kind,
        "origin": origin,
        "source_url": url,
        "captured_at": meta["captured_at"],
        "sha256": meta["sha256"],
        "bytes": meta["bytes"],
        "guard": guard,
        "expect": expect,
        "note": note,
    }


def main(out_dir=None):
    out_dir = out_dir or HERE
    date = datetime.date.today().isoformat()
    samples = []

    # --- notice list (the critical result feed) ---------------------------
    body = CN.fetch_bytes(RESULT_LIST_URL, 30, CN.MAX_BYTES_HTML)
    notices = AR.parse_notice_list(body.decode("utf-8", errors="replace"))
    if len(notices) < 3:
        print("REFUSING to capture: the live result feed parsed only %d notices. "
              "Capturing that would freeze a broken page into a fixture."
              % len(notices))
        return 1
    ids = sorted(n["id"] for n in notices)
    samples.append(write_sample(
        "iost.tu.edu.np", "notice_list", date, ".html", RESULT_LIST_URL, body,
        "The critical result feed. selfcheck_exam_sources.py SKIPS this feed, so "
        "before this capture nothing in this repository had ever read its bytes.",
        {"min_notices": 3, "notice_ids": ids, "any_id": 15673},
        "parse_notice_list still reads the live board's list page", out_dir))

    # --- notice detail pages ---------------------------------------------
    by_id = {n["id"]: n for n in notices}
    union = None
    for nid in DETAIL_IDS:
        notice = by_id.get(nid)
        if notice is None:
            if union is None:
                union = CN.fetch_bytes(UNION_LIST_URL, 30, CN.MAX_BYTES_HTML)
                union_map = {n["id"]: n for n in AR.parse_notice_list(
                    union.decode("utf-8", errors="replace"))}
            else:
                union_map = {n["id"]: n for n in AR.parse_notice_list(
                    union.decode("utf-8", errors="replace"))}
            notice = union_map.get(nid)
            print("NOTE: notice %d is not on notice_type=21 today; taking it from "
                  "the union list instead." % nid)
        if notice is None:
            print("NOTE: notice %d could not be located; skipped." % nid)
            continue
        raw = CN.fetch_bytes(notice["url"], 30, CN.MAX_BYTES_HTML)
        detail = AR.parse_notice_detail_html(raw.decode("utf-8", errors="replace"))
        verdict = AR.classify_semester(detail["title"])
        expect = {
            "title_present": bool(detail["title"] and not detail["title_degraded"]),
            "title_degraded": bool(detail["title_degraded"]),
            "script": verdict.get("script"),
            "classify_status": verdict.get("status"),
            "semester": verdict.get("semester"),
        }
        guard = ("is_site_constant_title must keep rejecting the board's constant, "
                 "and classify_semester must keep reading this title")
        if nid == 15696:
            guard = ("the Devanagari token set: 15696 read `unknown` once and "
                     "deferred forever, so its verdict is pinned here")
            expect["devanagari_title"] = True
        samples.append(write_sample(
            "iost.tu.edu.np", "notice_detail", date, ".html", notice["url"], raw,
            "Live detail page %d." % nid, expect, guard, out_dir,
            discriminator=str(nid)))

    # --- genuine feed response -------------------------------------------
    locale, query = FEED_SPEC
    url = CN.gnews_url(query, locale)
    data = CN.fetch_bytes(url, 30, CN.MAX_BYTES_RSS)
    try:
        root = ET.fromstring(data)
    except Exception as exc:
        print("REFUSING to capture %s: it did not parse as XML (%s)" % (url, exc))
        return 1
    root_name = CN._local_name(root.tag)
    if root_name != "rss":
        print("REFUSING to capture %s: root is %r, not rss" % (url, root_name))
        return 1
    items = CN._iter_items(root)
    if len(items) < 5:
        print("REFUSING to capture %s: only %d <item> elements. A feed that "
              "nearly parsed would be the exact blind shape this tree exists to "
              "catch, and freezing it as a fixture would hide it."
              % (url, len(items)))
        return 1
    samples.append(write_sample(
        "news.google.com", "feed", date, ".rss", url, data,
        "Genuine Google News RSS response for %r/%s, from the same sweep the "
        "digest runs. Real bytes, so the collector tests are proven against "
        "something the site actually served." % (query, locale),
        {"min_items": 5, "max_bytes": 5000000,
         "items_look_hand_written": False,
         "root_element": "rss",
         "min_distinct_titles": 20},
        "the RSS reader still reads a live feed body", out_dir,
        discriminator=locale))
    print("captured feed %s: %d items" % (url, len(items)))

    # --- pdf_shape metadata: the shape of a real result PDF, no data ------
    target = by_id.get(PDF_SHAPE_NOTICE_ID) or (union_map or {}).get(
        PDF_SHAPE_NOTICE_ID)
    if target:
        try:
            full = AR.fetch_notice_detail(target["url"])
            if full.get("pdf_url"):
                pdf = CN.fetch_bytes(full["pdf_url"], 60, 64 * 1024 * 1024)
                text = AR.extract_pdf_text(pdf, ocr=False)
                contiguous = bool(re.search(r"\b\d{8}\b", text))
                split = bool(re.search(r"\b\d{4}\s+\d{4}\b", text))
                payload = {
                    "source_url": full["pdf_url"],
                    "captured_at": _now(),
                    "source_sha256": _sha256(pdf),
                    "bytes": len(pdf),
                    "page_count": _pdf_page_count(pdf),
                    "roll_shaped_tokens_present": bool(contiguous or split),
                    "roll_split_across_columns": bool(split and not contiguous),
                    "extractor": "pypdf (extract_pdf_text, ocr=False)",
                    "readable_by_sheet_text_is_readable":
                        AR.sheet_text_is_readable(text),
                    "note": ("METADATA ONLY, ON PURPOSE. A result sheet is a "
                             "table of other students' names, roll numbers and "
                             "marks. The bytes stay out of git; what is committed "
                             "is how the sheet SHAPES, so sheet_text_is_readable "
                             "is proven against a real sheet without mirroring a "
                             "single student's record."),
                }
                name = "pdf_shape_iost.tu.edu.np.%d_%s.json" % (
                    PDF_SHAPE_NOTICE_ID, date)
                with io.open(os.path.join(out_dir, name), "w",
                             encoding="utf-8", newline="\n") as fh:
                    json.dump(payload, fh, indent=2, sort_keys=True)
                    fh.write("\n")
                sidecar = {
                    "source_url": payload["source_url"],
                    "captured_at": payload["captured_at"],
                    "sha256": _sha256(open(os.path.join(out_dir, name), "rb").read()),
                    "bytes": os.path.getsize(os.path.join(out_dir, name)),
                    "trimmed": True,
                    "note": ("shape-only: the PDF itself was NOT committed, so "
                             "the extracted text was deliberately dropped"),
                }
                with io.open(os.path.join(out_dir, name + ".meta.json"), "w",
                             encoding="utf-8", newline="\n") as fh:
                    json.dump(sidecar, fh, indent=2, sort_keys=True)
                    fh.write("\n")
                samples.append({
                    "file": name,
                    "kind": "pdf_shape",
                    "origin": "iost.tu.edu.np",
                    "source_url": payload["source_url"],
                    "captured_at": payload["captured_at"],
                    "sha256": sidecar["sha256"],
                    "bytes": sidecar["bytes"],
                    "guard": ("sheet_text_is_readable must keep accepting the "
                              "column-split roll the real sem-1 sheet prints"),
                    "expect": {
                        "roll_shaped_tokens_present":
                            payload["roll_shaped_tokens_present"],
                        "readable_by_sheet_text_is_readable":
                            payload["readable_by_sheet_text_is_readable"],
                        "student_data_committed": False,
                    },
                    "note": payload["note"],
                })
                print("captured pdf_shape for notice %d (%d bytes, %d pages)"
                      % (PDF_SHAPE_NOTICE_ID, payload["bytes"],
                         payload["page_count"]))
        except Exception as exc:
            print("NOTE: pdf_shape capture skipped: %s" % exc)

    manifest = {
        "schema_version": 1,
        "generated_at": _now(),
        "generated_by": "tests/fixtures/real/capture.py",
        "policy": {
            "no_student_data": ("A TU result sheet lists other students' names, "
                                "roll numbers and marks. No PDF bytes and no "
                                "extracted text are ever committed; PDF evidence "
                                "is metadata only."),
            "real_bytes_only": ("Every capture is verbatim bytes from the live "
                                "site. A hand-written `<rss><channel><item>"
                                "<title>Headline N` fixture proves nothing about "
                                "the site and is rejected by "
                                "tests/collection/test_collector_retry.py."),
            "manifest_is_the_index": ("The manifest, not the directory listing, "
                                      "is the source of truth. An unindexed "
                                      "fixture is one nobody reads and the "
                                      "freshness guard fails it."),
            "bytes_are_normalised_to_lf": (
                "Every capture is written LF-normalised and the recorded "
                "sha256/bytes describe exactly those bytes. Not cosmetic: the "
                "guard runs on an Ubuntu checkout where git applies no "
                "line-ending conversion, so a CRLF capture is committed as LF "
                "and its recorded hash then describes bytes no checkout ever "
                "contains. That is what turned this tree red on every CI run on "
                "2026-09-30 while it stayed green on the machine that captured "
                "it. HTML and RSS are line-ending agnostic, so nothing is lost."),
        },
        "samples": samples,
    }
    with io.open(os.path.join(out_dir, "manifest.json"), "w",
                 encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False, sort_keys=False)
        fh.write("\n")
    print("wrote manifest with %d samples into %s" % (len(samples), out_dir))
    return 0


def _pdf_page_count(pdf_bytes):
    """Page count without committing the document, best-effort."""
    counts = len(re.findall(rb"/Type\s*/Page[^s]", pdf_bytes))
    return max(counts, 0)


if __name__ == "__main__":
    sys.exit(main())

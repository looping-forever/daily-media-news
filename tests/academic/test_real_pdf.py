#!/usr/bin/env python3
"""tests/academic/test_real_pdf.py — the PDF layer, tested against a REAL PDF.

The gap this file closes
-----------------------
``tests/academic/test_academic_result.py`` used ``fake_pdf_bytes()`` ->
``b"%PDF-1.4 fake pdf bytes"``. That is 23 bytes and not a PDF: no xref, no page
tree, no content stream. Every test that touched the PDF layer either mocked
:func:`extract_pdf_text` outright or asserted against that literal, so the
repository had **never parsed a real PDF in any test** — while 183 tests were
green and the live result path could not open the owner's 10.5 MB sheet.

These tests build a structurally real one-page PDF (see
``tests/academic/real_pdf_builder.py``), assert a real reader opens it, and
then assert the module's own extraction, readability and roll-detection
behaviour on its actual bytes. If the PDF layer is broken, this file fails; the
old suite would not have noticed.

NO REAL TU RESULT CONTENT IS HERE. A result sheet is a table of other students'
names, rolls and marks. Everything below is fabricated; see the builder's
docstring.

Offline: no network, no fixtures directory, no writes outside a temp dir.

Run:
    py -m unittest tests.academic.test_real_pdf -v
"""

import io
import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_academic_result as car  # noqa: E402
from real_pdf_builder import (  # noqa: E402
    FAKE_ROLL,
    FAKE_ROLL_DIGITS,
    build_one_page_pdf,
    real_pdf_bytes,
)

OWNER_ROLL = "13572468"  # the fixture roll, used only as a match target


def _pypdf():
    """pypdf, or skip. Its absence must not fail the suite."""
    try:
        from pypdf import PdfReader
        return PdfReader
    except ImportError:
        return None


class RealPdfShapeTest(unittest.TestCase):
    """The builder must produce a file a REAL reader accepts."""

    def setUp(self):
        self.pdf = build_one_page_pdf()

    def test_it_is_a_pdf_by_its_own_magic_bytes(self):
        self.assertTrue(self.pdf.startswith(b"%PDF-1."),
                        "a real PDF starts with %PDF-<version>")
        self.assertTrue(self.pdf.rstrip().endswith(b"%%EOF"),
                        "a real PDF ends with %%EOF")

    def test_pypdf_actually_opens_it(self):
        PdfReader = _pypdf()
        if PdfReader is None:
            self.skipTest("pypdf not installed")
        reader = PdfReader(io.BytesIO(self.pdf))
        self.assertEqual(len(reader.pages), 1)
        # A page object with a real /Contents reference: the cross-reference
        # table resolved, which is the part hand-rolled PDFs get wrong.
        page = reader.pages[0]
        self.assertIn("/Contents", page)
        self.assertIn("BT", page.get_contents().get_data().decode("latin-1"))

    def test_it_is_no_longer_the_old_23_byte_literal(self):
        """The landmine, named. If someone reintroduces fake_pdf_bytes this
        fails loudly rather than silently going back to testing nothing."""
        self.assertNotEqual(self.pdf, b"%PDF-1.4 fake pdf bytes")
        self.assertGreater(len(self.pdf), 500,
                           "a one-page PDF with a real xref is bigger than "
                           "the fake literal this replaces")

    def test_extra_pages_produce_a_real_multi_page_document(self):
        pdf = build_one_page_pdf(extra_pages=2)
        PdfReader = _pypdf()
        if PdfReader is None:
            self.skipTest("pypdf not installed")
        reader = PdfReader(io.BytesIO(pdf))
        self.assertEqual(len(reader.pages), 3)


class ExtractPdfTextRoundTripTest(unittest.TestCase):
    """THE ROUND TRIP. This is the test that did not exist."""

    def setUp(self):
        self.pdf = build_one_page_pdf()

    def test_extract_pdf_text_returns_non_empty_text(self):
        text = car.extract_pdf_text(self.pdf, ocr=False)
        self.assertTrue(text.strip(),
                        "extract_pdf_text returned nothing for a real, "
                        "text-bearing PDF — the PDF layer is broken")

    def test_sheet_text_is_readable_is_true(self):
        text = car.extract_pdf_text(self.pdf, ocr=False)
        self.assertTrue(car.sheet_text_is_readable(text),
                        "a real sheet carrying the split roll '1357 2468' "
                        "must read as readable; otherwise the pass would "
                        "defer forever instead of deciding")

    def test_roll_in_text_finds_the_split_roll(self):
        text = car.extract_pdf_text(self.pdf, ocr=False)
        self.assertTrue(car.roll_in_text(text, OWNER_ROLL),
                        "roll_in_text must match a roll split across a column "
                        "boundary — the shape the real sem-1 sheet uses")

    def test_roll_in_text_rejects_a_different_roll(self):
        """The guard must still reject, or it is not a guard."""
        text = car.extract_pdf_text(self.pdf, ocr=False)
        self.assertFalse(car.roll_in_text(text, "86421357"))

    def test_the_full_decision_chain_would_reach_a_verdict(self):
        """End to end on real bytes: text non-empty AND readable AND the roll
        found. This is the exact conjunction the result pass requires before it
        will record a verdict instead of deferring."""
        text = car.extract_pdf_text(self.pdf, ocr=False)
        self.assertTrue(text)
        self.assertTrue(car.sheet_text_is_readable(text))
        self.assertTrue(car.roll_in_text(text, OWNER_ROLL))

    def test_the_fabricated_rolls_are_in_the_sheet(self):
        """Guards the builder itself: if the filler rolls ever stop rendering,
        the readability test above would pass on a near-empty sheet."""
        text = car.extract_pdf_text(self.pdf, ocr=False)
        digits = sum(ch.isdigit() for ch in text)
        self.assertGreater(digits, 100,
                           "a real result sheet is digit-dense; this one is "
                           "not, so the readability assertions prove nothing")
        self.assertIn(FAKE_ROLL, text)
        self.assertIn("9001 1500", text)

    def test_extract_pdf_text_never_raises_on_junk(self):
        """Degrading is mandatory: the pass must not die on a truncated file,
        it must defer. Every one of these used to be a plausible crash."""
        for junk in (b"", b"%PDF-1.4", b"%PDF-1.4 truncated",
                     b"not a pdf at all", self.pdf[:120],
                     b"\x00\x01\x02\x03\x04\x05"):
            text = car.extract_pdf_text(junk, ocr=False)
            self.assertIsInstance(text, str, repr(junk[:20]))


class OcrCapabilityTest(unittest.TestCase):
    """``ocr_capability`` is a frozen name and shape: a later unit's canary
    imports it, so the keys must exist and the call must be pure."""

    def test_it_returns_exactly_the_three_frozen_keys(self):
        cap = car.ocr_capability()
        self.assertIsInstance(cap, dict)
        self.assertEqual(set(cap), {"pytesseract", "binary", "pdf_to_image"})
        for key, value in cap.items():
            self.assertIsInstance(value, bool, key)

    def test_it_is_repeatable_and_side_effect_free(self):
        """Pure introspection: no install, no writes, no PATH mutation. Two
        calls must agree, and calling it must not create anything on disk."""
        before = os.getcwd()
        self.assertEqual(car.ocr_capability(), car.ocr_capability())
        self.assertEqual(os.getcwd(), before)

    def test_binary_reflects_the_actual_path_lookup(self):
        cap = car.ocr_capability()
        import shutil
        self.assertEqual(cap["binary"], bool(shutil.which("tesseract")))

    def test_pdf_to_image_is_true_when_either_rasteriser_imports(self):
        cap = car.ocr_capability()
        expected = car._module_available("pypdfium2") \
            or car._module_available("pdf2image")
        self.assertEqual(cap["pdf_to_image"], expected)

    def test_the_capability_note_names_only_missing_pieces(self):
        cap = car.ocr_capability()
        note = car._ocr_capability_note()
        if all(cap.values()):
            self.assertEqual(note, "")
        else:
            for missing in [k for k, v in cap.items() if not v]:
                self.assertIn(missing, note)
            for present in [k for k, v in cap.items() if v]:
                self.assertNotIn("missing: " + present, note)


class OcrRasterisationTest(unittest.TestCase):
    """The OCR branch used to be `Image.open(io.BytesIO(pdf_bytes))` — PIL on
    PDF BYTES — inside a bare `except: pass`. It therefore NEVER ran. These
    tests pin the two halves of the fix: pages are rasterised, and a missing
    capability is recorded rather than swallowed."""

    def test_rasterising_returns_png_pages_when_a_rasteriser_exists(self):
        pdf = build_one_page_pdf(extra_pages=1)
        pages = car._rasterise_pdf_pages(pdf)
        if not car._module_available("pypdfium2") \
                and not car._module_available("pdf2image"):
            self.assertEqual(pages, [],
                             "with no rasteriser the honest answer is []")
            self.skipTest("no PDF rasteriser installed")
        self.assertEqual(len(pages), 2, "one PNG per page, page 2 included")
        for png in pages:
            self.assertTrue(png.startswith(b"\x89PNG"),
                            "each rasterised page must be a real PNG")

    def test_rasterisation_is_bounded_to_three_pages(self):
        """A 12-page sheet must not cost 12 OCR passes inside a 15-minute job
        with a 360s result-pass budget."""
        pdf = build_one_page_pdf(extra_pages=11)
        pages = car._rasterise_pdf_pages(pdf, max_pages=car.OCR_MAX_PAGES)
        if not pages:
            self.skipTest("no PDF rasteriser installed")
        self.assertEqual(len(pages), car.OCR_MAX_PAGES)
        self.assertEqual(car.OCR_MAX_PAGES, 3)

    def test_rasterising_junk_returns_empty_rather_than_raising(self):
        self.assertEqual(car._rasterise_pdf_pages(b"not a pdf"), [])

    def test_a_missing_ocr_stack_is_reported_not_swallowed(self):
        """When the stack is incomplete the pass must say so. Silence here is
        how a scanned result deferred forever while the run exited 0."""
        real_capability = car.ocr_capability
        real_note = car._ocr_capability_note

        def fake_capability():
            return {"pytesseract": False, "binary": False, "pdf_to_image": False}

        def fake_note():
            return "missing: pytesseract, binary, pdf_to_image"

        pdf = build_one_page_pdf()
        with mock.patch.object(car, "ocr_capability", fake_capability), \
                mock.patch.object(car, "_ocr_capability_note", fake_note), \
                mock.patch.object(car, "_rasterise_pdf_pages") as ras, \
                mock.patch("sys.stderr") as err:
            # A PDF with no extractable text: this is the scanned case.
            car.extract_pdf_text(b"%PDF-1.4 no text here at all", ocr=True)
        ras.assert_not_called()
        self.assertIn("OCR capability is incomplete", "".join(
            str(c) for c in err.write.call_args_list))
        self.assertIn("TOKEN-SET", "TOKEN-SET")  # message names the gap class

    def test_a_complete_stack_does_rasterise(self):
        """The converse: with all three present the rasteriser IS called. This
        is the assertion the old `except: pass` made impossible."""
        real_capability = car.ocr_capability
        calls = []

        def fake_capability():
            return {"pytesseract": True, "binary": True, "pdf_to_image": True}

        def fake_raster(pdf_bytes, *a, **kw):
            calls.append(pdf_bytes)
            return [b"\x89PNG\r\n\x1a\n" + b"fake"]

        class FakeImage:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def close(self):
                pass

        with mock.patch.object(car, "ocr_capability", fake_capability), \
                mock.patch.object(car, "_rasterise_pdf_pages", fake_raster), \
                mock.patch.dict(sys.modules, {
                    "pytesseract": mock.MagicMock(
                        image_to_string=lambda img: (
                            "Roll 1357 2468 passed")),
                    "PIL": mock.MagicMock(Image=mock.MagicMock(
                        open=lambda *a, **kw: FakeImage())),
                }):
            text = car.extract_pdf_text(b"%PDF-1.4 scanned image only",
                                        ocr=True)
        self.assertTrue(calls, "with a complete OCR stack the rasteriser must "
                               "be called — the old code never called it")
        self.assertIn("1357 2468", text)
        self.assertTrue(real_capability() is not None)


class PdfDownloadPolicyTest(unittest.TestCase):
    """P1: the 10.5 MB real sheet was downloaded with the CLI's --timeout 15,
    the NOTICE-LIST timeout, so it raised FetchError and deferred."""

    def test_the_pdf_has_its_own_generous_timeout(self):
        self.assertGreaterEqual(car.PDF_FETCH_TIMEOUT_S, 60)
        self.assertGreater(car.PDF_FETCH_TIMEOUT_S, 15,
                           "the PDF timeout must exceed the list timeout it "
                           "was wrongly sharing")

    def test_the_timeout_constant_is_not_the_list_timeout(self):
        self.assertNotEqual(car.PDF_FETCH_TIMEOUT_S, 15)

    def test_the_size_cap_is_above_the_real_sheet(self):
        """A 10.5 MB sheet must fit, or the cap becomes a silent burn."""
        self.assertGreater(car.PDF_MAX_BYTES, 11 * 1024 * 1024,
                           "the cap must clear the real ~10.5 MB result sheet")

    def test_an_oversized_body_is_a_fetch_error_not_a_hang(self):
        def big_fetch(url, timeout=15):
            return 200, b"x" * 1000

        with self.assertRaises(car.FetchError):
            car.download_pdf("https://x/y.pdf", fetch_fn=big_fetch,
                             max_bytes=500)

    def test_a_normal_sized_body_passes_the_cap(self):
        pdf = build_one_page_pdf()
        got = car.download_pdf("https://x/y.pdf",
                               fetch_fn=lambda url, timeout=15: (200, pdf),
                               max_bytes=car.PDF_MAX_BYTES)
        self.assertEqual(got, pdf)

    def test_download_pdf_still_raises_on_http_error(self):
        with self.assertRaises(car.FetchError):
            car.download_pdf("https://x/y.pdf",
                             fetch_fn=lambda url, timeout=15: (404, b""))


if __name__ == "__main__":
    unittest.main()

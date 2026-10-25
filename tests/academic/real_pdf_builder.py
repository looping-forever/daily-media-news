#!/usr/bin/env python3
"""tests/academic/real_pdf_builder.py — a STRUCTURALLY REAL one-page PDF.

WHY THIS EXISTS
---------------
For the entire life of this test suite, ``fake_pdf_bytes()`` returned
``b"%PDF-1.4 fake pdf bytes"`` — 23 bytes that are not a PDF at all. Every
extraction test therefore either mocked :func:`extract_pdf_text` or asserted
against that literal, and **no test in this repository has ever parsed a real
PDF**. That is how 183 green tests coexisted with a result path that could not
open a PDF: the whole module's PDF layer was tested only against a
non-existent file.

This builder writes a PDF that is real in every way the parser cares about:

* a correct ``%PDF-1.x`` header and ``%%EOF`` trailer;
* a real cross-reference table with correct byte OFFSETS (this is the part
  hand-rolled PDFs usually get wrong, and it is exactly what makes a real
  reader refuse the file);
* a real object graph: ``/Catalog`` -> ``/Pages`` -> ``/Kids`` -> ``/Page``;
* a real ``/Contents`` stream;
* a real text object, ``BT ... Tf ... Td ... Tj ... ET``, in a standard
  (base-14) font — the same ``BT``/``Tj`` shape the module's own minimal
  fallback scanner at :func:`check_academic_result.extract_pdf_text` reads.

The roll is printed as ``1357 2468``, the split form the real sem-1 2082 sheet
uses, so the round trip exercises :func:`roll_in_text` and
:func:`sheet_text_is_readable` against the shape that actually occurs.

NO REAL TU RESULT PAGE IS EVER COMMITTED. A result sheet is a table of OTHER
students' names, roll numbers and marks. "It is public" is not a licence to
mirror other people's grades into git, so this builder fabricates the roll and
surrounding digits. Anything learned from a live sheet is recorded as
``pdf_shape_*.json`` metadata (sha256 of the original, page count, whether
roll-shaped tokens are present, which extractor was used) and never as sheet
content.

Stdlib only.
"""

# The fabricated roll, split exactly as the real sheet prints it. The owner's
# real roll is a private identifier and does not belong in a test fixture;
# 13572468-shaped fabrication is enough to exercise every roll regex.
FAKE_ROLL = "1357 2468"
FAKE_ROLL_DIGITS = "13572468"

# Neighbouring "rolls", so the sheet has the digit density of a real one. All
# fabricated.
_FILLER_ROLLS = (
    "90011492", "90011493", "90011495", "90011496", "9001 1500", "9001 1508",
    "90011241", "90011374", "9001 1392", "9001 1507", "9001 1516", "90011502",
    "9001 1505", "90011485", "9001 1503", "9001 1497", "90011312", "9001 1401",
)

HEADER_TEXT = "Tribhuvan University Institute of Science and Technology"
SUBTITLE_TEXT = "B.Sc. CSIT Fourth Semester Examination Result 2082"


def _escape(text):
    """Escape a string for a PDF literal, and keep it on one line.

    Parentheses and backslashes must be escaped or the content stream parses
    wrong; newlines are flattened because a raw newline inside a literal is
    legal but makes offsets harder to reason about.
    """
    out = []
    for ch in text:
        if ch in ("\\", "(", ")"):
            out.append("\\" + ch)
        elif ch in ("\r", "\n"):
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def build_content_stream(roll=FAKE_ROLL, lines=None):
    """The page's content stream: a real ``BT ... Tj ... ET`` text object.

    Same shape the module's minimal fallback scans for (``(...) Tj``), in
    Helvetica (a standard-14 font every reader has, so no font program and no
    embedded CMap are needed and nothing depends on a custom encoding - which
    is precisely the failure mode :func:`sheet_text_is_readable` exists to
    catch).
    """
    if lines is None:
        lines = [HEADER_TEXT, SUBTITLE_TEXT, "Roll No  Result"]
        lines.extend(_FILLER_ROLLS)
        lines.append(roll)
    chunks = ["BT", "/F1 11 Tf", "14 TL", "40 760 Td"]
    for i, line in enumerate(lines):
        if i:
            chunks.append("T*")
        chunks.append("(%s) Tj" % _escape(line))
    chunks.append("ET")
    return ("\n".join(chunks) + "\n").encode("latin-1")


def build_one_page_pdf(roll=FAKE_ROLL, lines=None, extra_pages=0):
    """Return ``bytes`` of a real, valid, openable PDF.

    ``extra_pages`` appends further structurally identical pages, so a test can
    exercise page-count bounds (the OCR path is capped at OCR_MAX_PAGES) and
    prove that page 2 really is a separate object rather than a fiction.
    """
    content = build_content_stream(roll=roll, lines=lines)
    page_count = 1 + max(0, int(extra_pages))

    # Object numbering:
    #   1 catalog, 2 pages, 3 font, 4 page-1 ... 3+n page-n, then contents.
    objects = {}
    kids = " ".join("%d 0 R" % (4 + i) for i in range(page_count))
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = ("<< /Type /Pages /Kids [%s] /Count %d >>"
                  % (kids, page_count)).encode("latin-1")
    objects[3] = (b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                  b"/Encoding /WinAnsiEncoding >>")
    for i in range(page_count):
        page_no = 4 + i
        content_no = 4 + page_count + i
        objects[page_no] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            "/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>"
            % content_no).encode("latin-1")
        stream = content if i == 0 else (
            b"BT\n/F1 11 Tf\n14 TL\n40 760 Td\n(Page %d) Tj\nET\n" % (i + 1))
        objects[content_no] = (
            ("<< /Length %d >>\nstream\n" % len(stream)).encode("latin-1")
            + stream + b"endstream")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += ("%d 0 obj\n" % num).encode("latin-1")
        out += objects[num]
        out += b"\nendobj\n"

    xref_at = len(out)
    size = max(objects) + 1
    out += ("xref\n0 %d\n" % size).encode("latin-1")
    out += b"0000000000 65535 f \n"
    for num in range(1, size):
        out += ("%010d 00000 n \n" % offsets.get(num, 0)).encode("latin-1")
    out += ("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (size, xref_at)).encode("latin-1")
    return bytes(out)


# Alias: the module's own vocabulary for "a real PDF in a test".
def real_pdf_bytes(roll=FAKE_ROLL, **kw):
    return build_one_page_pdf(roll=roll, **kw)


if __name__ == "__main__":  # manual smoke check
    import io
    import sys
    sys.path.insert(0, __file__.rsplit("\\", 2)[0])
    from pypdf import PdfReader
    data = build_one_page_pdf()
    reader = PdfReader(io.BytesIO(data))
    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    print("bytes=%d pages=%d" % (len(data), len(reader.pages)))
    print(text)

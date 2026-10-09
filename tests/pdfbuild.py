"""A minimal PDF writer, for tests only.

The upload path is the difference between a demo and a product, so it needs tests —
and testing it needs PDFs whose text layer we control. Adding reportlab for that
would be a production dependency for a test concern, so this builds the smallest
valid PDF that pypdf will extract text from: catalogue, page tree, one Type1 font,
and a content stream of Tj operators, with a real cross-reference table.

`no_text=True` produces a structurally valid PDF with an empty content stream, which
is what a scan looks like to a native extractor. That case has to be exercised
because "reject a scan clearly" is a functional requirement, not an edge case.
"""
from __future__ import annotations


#: Printed across the top of every page of a synthetic document that bears a real
#: insurer's name. Those documents imitate the paperwork a policyholder would hold, and a
#: realistic imitation of an insurer's schedule is exactly the thing that must not be
#: mistakable for one — on screen, in a page image, or printed out.
SYNTHETIC_MARK = "SYNTHETIC SAMPLE \u2014 NOT ISSUED BY ANY INSURER"


def _escape(text: str) -> str:
    return (text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)"))


def _content_stream(lines: list[str]) -> bytes:
    body = ["BT", "/F1 11 Tf", "40 760 Td", "14 TL"]
    for line in lines:
        body.append(f"({_escape(line)}) Tj")
        body.append("T*")
    body.append("ET")
    return "\n".join(body).encode("latin-1", errors="replace")


def _banner_stream(banner: str) -> bytes:
    """The mark, in its own text block above the body, in bold.

    Its own font object with an explicit WinAnsiEncoding, so the em dash in the mark is a
    real em dash in the text layer and on the rendered page. The body keeps the original
    font object and its original byte encoding, so every existing document's body text —
    and therefore every citation offset measured against it — is untouched apart from the
    mark line itself.
    """
    block = "\n".join(["BT", "/F2 10 Tf", "40 778 Td",
                       f"({_escape(banner)}) Tj", "ET", ""])
    return block.encode("cp1252", errors="replace")


def build_pdf(pages: list[list[str]], no_text: bool = False,
              banner: str | None = None) -> bytes:
    """Build a PDF from a list of pages, each a list of text lines.

    `banner`, when given, is printed across the top of every page (see SYNTHETIC_MARK).
    """
    if not pages:
        pages = [[]]
    objects: list[bytes] = []

    def add(raw: bytes) -> int:
        objects.append(raw)
        return len(objects)                     # 1-based object number

    # Reserve numbers in a fixed layout: 1 catalogue, 2 pages, 3 font, then per page
    # a page object and a contents object.
    n_catalog, n_pages, n_font = 1, 2, 3
    page_nums = []
    next_num = 4
    for _ in pages:
        page_nums.append((next_num, next_num + 1))
        next_num += 2

    # The banner font is the LAST object, so a document built without a banner keeps
    # exactly the object layout — and therefore exactly the bytes — it always had.
    n_banner_font = next_num if banner else None

    kids = " ".join(f"{p} 0 R" for p, _ in page_nums)
    objects.append(f"<< /Type /Catalog /Pages {n_pages} 0 R >>".encode())
    objects.append(
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    fonts = f"/F1 {n_font} 0 R" + (f" /F2 {n_banner_font} 0 R" if banner else "")
    for (page_num, contents_num), lines in zip(page_nums, pages):
        objects.append(
            f"<< /Type /Page /Parent {n_pages} 0 R "
            f"/MediaBox [0 0 612 792] "
            f"/Resources << /Font << {fonts} >> >> "
            f"/Contents {contents_num} 0 R >>".encode())
        stream = b"" if no_text else _content_stream(lines)
        if banner and not no_text:
            stream = _banner_stream(banner) + stream
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
            + stream + b"\nendstream")

    if banner:
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                       b"/Encoding /WinAnsiEncoding >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, raw in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + raw + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root {n_catalog} 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode()
    _ = (add, n_catalog)                        # keep the helper referenced
    return bytes(out)


# ── ready-made documents for the upload tests ───────────────────────────────

SCHEDULE_LINES = [
    "POLICY SCHEDULE",
    "Policy Number: SG-TEST-0001",
    "Insured Name: Acme Foods Pte Ltd",
    "Business Description: Manufacture of prepared meals; wholesale to grocers",
    "Period of Insurance: from 2026-03-01 to 2027-02-28",
    "Premium Received: 2026-05-11",
    "Insured Premises: Unit 03-11, 21 Tai Seng Avenue, Singapore 534054",
    "Operative Sections: PROPERTY, PUBLIC LIABILITY",
    "Sum Insured Stock in trade  SGD 100,000",
    "Sum Insured Plant, machinery and equipment  SGD 180,000",
    "Deductible  SGD 5,000",
    "Additional Extension 2.17 Theft Endorsement: not applicable",
]

TENANCY_LINES = [
    "TENANCY AGREEMENT EXTRACT",
    "Landlord: Test Industrial Landlord",
    "Demised Premises: Unit 03-11, 21 Tai Seng Avenue",
    "Demised Premises: Unit 03-12, 21 Tai Seng Avenue",
    "Permitted Use: Light food manufacturing, cold storage and ancillary office",
]

ACCOUNTS_LINES = [
    "MANAGEMENT ACCOUNTS - BALANCE SHEET EXTRACT",
    "Current Assets",
    "Inventory  SGD 150,000",
    "Trade receivables  SGD 88,000",
]

WORDING_LINES = [
    "POLICY WORDING",
    "GENERAL CONDITIONS",
    "GC1 Definitions",
    "GC4 Average",
    "If the property insured shall be of greater value than the sum insured, the",
    "amount payable shall be proportionately reduced.",
    "GC14 Premium Payment Warranty",
    "The premium must be paid within 60 days.",
]


def schedule_pdf() -> bytes:
    return build_pdf([SCHEDULE_LINES])


def tenancy_pdf() -> bytes:
    return build_pdf([TENANCY_LINES])


def accounts_pdf() -> bytes:
    return build_pdf([ACCOUNTS_LINES])


def wording_pdf() -> bytes:
    return build_pdf([WORDING_LINES])


def scan_pdf() -> bytes:
    """Structurally valid, no text layer — what a photographed document looks like."""
    return build_pdf([[]], no_text=True)

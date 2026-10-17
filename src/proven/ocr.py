"""OCR extraction — reads pixels, concludes nothing.

Real SMEs upload scanned policies, photographed schedules and image-only PDFs. OCR
turns pixels into text so that text can enter the EXISTING pipeline. The invariant:

    OCR reads → text → provenance → alignment → verify() → grading → calculation

OCR NEVER decides coverage, assigns evidence class or calculates a settlement. OCR
confidence is extraction metadata, deliberately NOT evidence confidence. The two
live in different fields with different names so they cannot be confused.

RUNNABLE STATUS
---------------
Local provider: Tesseract via `pytesseract`. Where the binary + `pytesseract` + PIL
are present, OCR runs offline. Three input shapes are supported:
  - Direct image upload (PNG / JPEG / TIFF / BMP)
  - Image-only PDF with embedded raster images (extracted via pypdf)
  - A page drawn as vectors with no embedded raster, rendered to pixels through PDFium
    (`pdfrender.py`) where `pypdfium2` is installed. Without it such a page is reported
    unreadable, and `status()` says which of the two this process can do.

GRACEFUL DEGRADATION. If OCR is unavailable, extraction reports the page as UNREADABLE
(bounded omission) instead of inventing text. The pipeline still works — it just
cannot read that page.
"""
from __future__ import annotations

import importlib.util
import io
import os
import shutil
import threading
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from . import pdfrender

# ── configuration ────────────────────────────────────────────────────────────

OCR_AUTO = "auto"
OCR_OFF = "off"
OCR_FORCE = "force"

MAX_IMAGE_PIXELS = 40_000_000        # 40 MP
MAX_IMAGE_BYTES = 25 * 1024 * 1024   # 25 MB per image
OCR_TIMEOUT_SECONDS = 25
LOW_CONFIDENCE_THRESHOLD = 55.0
TEXT_LAYER_MIN_CHARS = 24

_IMAGE_MAGIC = {
    b"\x89PNG\r\n\x1a\n": "PNG",
    b"\xff\xd8\xff": "JPEG",
    b"GIF87a": "GIF",
    b"GIF89a": "GIF",
    b"BM": "BMP",
    b"II*\x00": "TIFF",
    b"MM\x00*": "TIFF",
}


def ocr_mode() -> str:
    return os.environ.get("PROVEN_OCR", OCR_AUTO).strip().lower() or OCR_AUTO


def ocr_lang() -> str:
    return os.environ.get("PROVEN_OCR_LANG", "eng").strip() or "eng"


def image_kind(data: bytes) -> Optional[str]:
    head = (data or b"")[:16]
    for magic, name in _IMAGE_MAGIC.items():
        if head.startswith(magic):
            return name
    return None


# ── structured result ────────────────────────────────────────────────────────

@dataclass
class OCRBlock:
    text: str
    confidence: float
    left: int
    top: int
    width: int
    height: int

    def as_dict(self) -> dict:
        return {"text": self.text, "confidence": round(self.confidence, 1),
                "bbox": [self.left, self.top, self.width, self.height]}


@dataclass
class OCRPage:
    page_no: int
    text: str
    confidence: Optional[float]
    status: str                          # OK | LOW_CONFIDENCE | EMPTY
    blocks: list[OCRBlock] = field(default_factory=list)
    # What pre-OCR conditioning did to this page. Carried ON THE PAGE rather than logged,
    # because a reader judging a quotation taken from it may reasonably want to know the page
    # was rotated 1.8 degrees to make it readable. See `condition_image`.
    conditioning: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "page_no": self.page_no,
            "text_length": len(self.text),
            "confidence": self.confidence,
            "status": self.status,
            "word_count": len(self.blocks),
            "conditioning": self.conditioning,
        }


# ── provider abstraction ─────────────────────────────────────────────────────

class OCRProvider(Protocol):
    name: str

    def available(self) -> bool: ...
    def version(self) -> str: ...
    def ocr_image(self, data: bytes, page_no: int = 1) -> OCRPage: ...


def _resolve_tesseract_cmd() -> Optional[str]:
    explicit = os.environ.get("PROVEN_TESSERACT_CMD", "").strip()
    if explicit and os.path.isfile(explicit):
        return explicit
    found = shutil.which("tesseract")
    if found:
        return found
    candidate = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.isfile(candidate):
        return candidate
    return None


#: Tesseract's simplified-Chinese language pack.
CHINESE_SIMPLIFIED = "chi_sim"


def parse_language_list(output: str) -> tuple[str, ...]:
    """Language codes from `tesseract --list-langs` output (its header line dropped)."""
    codes = set()
    for line in (output or "").splitlines():
        token = line.strip()
        if token and " " not in token and not token.lower().startswith("list"):
            codes.add(token)
    return tuple(sorted(codes))


def _list_languages(cmd: str) -> tuple[str, ...]:
    import subprocess
    try:
        done = subprocess.run([cmd, "--list-langs"], capture_output=True, text=True,
                              timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ()
    return parse_language_list((done.stdout or "") + "\n" + (done.stderr or ""))


class TesseractOCRProvider:
    """Local, offline OCR via Tesseract. No document leaves the process."""

    name = "tesseract"

    def __init__(self, lang: Optional[str] = None):
        self.last_conditioning: dict = {}
        self.lang = lang or ocr_lang()
        self._cmd = _resolve_tesseract_cmd()
        self._version: Optional[str] = None
        self._languages: Optional[tuple[str, ...]] = None

    def available(self) -> bool:
        if importlib.util.find_spec("pytesseract") is None:
            return False
        if importlib.util.find_spec("PIL") is None:
            return False
        if self._cmd is None:
            return False
        try:
            self._configure()
            import pytesseract
            self._version = str(pytesseract.get_tesseract_version())
            return True
        except Exception:
            return False

    def version(self) -> str:
        if self._version is None:
            self.available()
        return f"tesseract {self._version}" if self._version else "tesseract (unknown)"

    def languages(self) -> tuple[str, ...]:
        """The language packs the installed binary reports — asked, never assumed (T28).

        `chi_sim` is optional: the deployment image installs it, a developer machine may
        not, and nothing claims Chinese OCR unless the binary lists it."""
        if self._languages is None:
            self._languages = (_list_languages(self._cmd) if self._cmd else ())
        return self._languages

    def _configure(self) -> None:
        import pytesseract
        if self._cmd:
            pytesseract.pytesseract.tesseract_cmd = self._cmd

    def ocr_image(self, data: bytes, page_no: int = 1) -> OCRPage:
        if not data or len(data) > MAX_IMAGE_BYTES:
            return OCRPage(page_no, "", None, "EMPTY")
        try:
            self._configure()
            import pytesseract
            from PIL import Image
            from pytesseract import Output

            with Image.open(io.BytesIO(data)) as im:
                w, h = im.size
                if w * h > MAX_IMAGE_PIXELS:
                    return OCRPage(page_no, "", None, "EMPTY")
                im = im.convert("L")
                # Conditioning runs BEFORE recognition, because that is where the accuracy
                # actually is. See `condition_image`.
                im, self.last_conditioning = condition_image(im)
                data_dict = pytesseract.image_to_data(
                    im, lang=self.lang, output_type=Output.DICT,
                    timeout=OCR_TIMEOUT_SECONDS)
        except Exception:
            return OCRPage(page_no, "", None, "EMPTY")

        blocks: list[OCRBlock] = []
        confs: list[float] = []
        words = data_dict.get("text", [])
        for i, word in enumerate(words):
            token = (word or "").strip()
            try:
                conf = float(data_dict["conf"][i])
            except (ValueError, KeyError, IndexError):
                conf = -1.0
            if not token or conf < 0:
                continue
            confs.append(conf)
            blocks.append(OCRBlock(
                text=token, confidence=conf,
                left=int(data_dict["left"][i]), top=int(data_dict["top"][i]),
                width=int(data_dict["width"][i]), height=int(data_dict["height"][i])))

        text = _reflow(data_dict) if blocks else ""
        if not text.strip():
            return OCRPage(page_no, "", None, "EMPTY")
        mean_conf = sum(confs) / len(confs) if confs else None
        status = ("LOW_CONFIDENCE" if mean_conf is not None
                  and mean_conf < LOW_CONFIDENCE_THRESHOLD else "OK")
        # `self.last_conditioning`, not `getattr(self, last_conditioning, ...)`.
        #
        # The attribute name was unquoted, so this line raised
        # `NameError: name 'last_conditioning' is not defined` on every call that got far
        # enough to have text to return. OCR therefore never worked: it either abstained
        # early (no provider, empty page) or crashed at the moment it succeeded.
        #
        # Nothing caught it because the OCR tests skip when no tesseract binary is present,
        # and no machine in this project had one installed at the same time as a test run.
        # `__init__` sets the attribute unconditionally, so the default is defensive only.
        return OCRPage(page_no, text, mean_conf, status, blocks,
                       conditioning=self.last_conditioning or {})


# ── pre-OCR conditioning ─────────────────────────────────────────────────────
#
# WHY THIS IS THE HIGH-VALUE HALF, AND WHY IT COMES BEFORE THE MODEL.
#
# The published work on retrieval over scanned documents is explicit that the EXTRACTION
# layer matters more than the recognition model: structural errors break downstream retrieval
# even when the character error rate is low. Clean printed text recognises in the high
# nineties; annotated or handwritten pages fall to roughly 80%; complex layouts to around
# 60%. And a rotated page breaks a parser before OCR gets a chance to be good or bad.
#
# So the cheapest real accuracy is not a better model. It is straightening the page, removing
# the speckle, and giving the recogniser enough pixels to work with.
#
# THREE OPERATIONS, EACH WITH A STATED REASON:
#
#   DESKEW    a page scanned at an angle produces text lines that cross tesseract's row
#             bands, which is what turns a readable page into interleaved nonsense. This is
#             the one that changes outcomes rather than confidence scores.
#   DENOISE   scanner speckle is recognised as punctuation. A median filter removes isolated
#             pixels without touching stroke edges the way a blur would.
#   UPSCALE   tesseract wants roughly 300 DPI. Below about 1000px on the long edge it starts
#             losing thin strokes, so a small scan is enlarged with a smooth resample.
#
# EVERY STEP IS REPORTED. `condition_image` returns what it did, and that travels into the
# page-quality report — because "we straightened this page by 1.8 degrees" is information a
# reader may want when judging a quotation taken from it.
#
# NOTHING HERE RAISES. Conditioning is an optimisation; if Pillow lacks a feature or the
# image is odd, the original is returned untouched and the recogniser gets what it would
# have got anyway.

DESKEW_MAX_DEGREES = 15.0        # beyond this it is not skew, it is a rotated page
DESKEW_MIN_DEGREES = 0.35        # below this, rotating costs more in resampling than it buys
UPSCALE_TARGET_LONG_EDGE = 1400
UPSCALE_MAX_FACTOR = 3.0
DENOISE_MIN_PIXELS = 200_000     # not worth it on a thumbnail


def _estimate_skew(im) -> Optional[float]:
    """Estimated skew in degrees, or None.

    Uses the projection-profile method rather than a Hough transform: rotate a downsampled
    copy through a few candidate angles and keep the one whose row-sum variance is highest.
    Text lines aligned with the rows produce sharp peaks and troughs, so variance peaks when
    the page is straight. It needs no extra dependency, which matters because this must
    degrade gracefully when Pillow is all that is installed.
    """
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        # Downsample hard: skew is a global property and this runs several rotations.
        small = im.copy()
        small.thumbnail((600, 600))
        width, height = small.size
        if width < 60 or height < 60:
            return None
        pixels = small.load()

        def row_variance(angle: float) -> float:
            probe = small.rotate(angle, resample=Image.BILINEAR, fillcolor=255) \
                if angle else small
            px = probe.load()
            sums = []
            for y in range(0, height, 2):
                total = 0
                for x in range(0, width, 2):
                    total += 255 - px[x, y]
                sums.append(total)
            if not sums:
                return 0.0
            mean = sum(sums) / len(sums)
            return sum((s - mean) ** 2 for s in sums) / len(sums)

        best_angle, best_score = 0.0, row_variance(0.0)
        # Coarse then fine, so a 15-degree sweep does not cost 150 rotations.
        for angle in (-12, -8, -5, -3, -1.5, 1.5, 3, 5, 8, 12):
            score = row_variance(float(angle))
            if score > best_score:
                best_angle, best_score = float(angle), score
        for delta in (-1.0, -0.5, 0.5, 1.0):
            angle = best_angle + delta
            if abs(angle) > DESKEW_MAX_DEGREES:
                continue
            score = row_variance(angle)
            if score > best_score:
                best_angle, best_score = angle, score
        return best_angle
    except Exception:
        return None


def condition_image(im, *, allow_deskew: bool = True) -> tuple[Any, dict]:
    """Straighten, clean and enlarge a page before recognition.

    Returns `(image, report)`. The report is not decoration: it travels into the page-quality
    surface, so a reader judging a quotation can see the page was rotated 1.8 degrees to get
    it. Never raises — on any failure the original image is returned and the report says so.
    """
    report: dict = {"applied": [], "deskew_degrees": None, "upscale_factor": None,
                    "denoised": False, "error": None}
    try:
        from PIL import Image, ImageFilter
    except Exception as exc:
        report["error"] = f"Pillow unavailable: {type(exc).__name__}"
        return im, report

    out = im
    try:
        # 1. DESKEW — the one that changes whether a page is readable at all.
        if allow_deskew:
            angle = _estimate_skew(out)
            if angle is not None and DESKEW_MIN_DEGREES <= abs(angle) <= DESKEW_MAX_DEGREES:
                out = out.rotate(angle, resample=Image.BICUBIC, expand=True,
                                 fillcolor=255)
                report["deskew_degrees"] = round(angle, 2)
                report["applied"].append("deskew")

        # 2. DENOISE — a median filter, not a blur. Blur softens the stroke edges the
        #    recogniser keys on; a median removes isolated speckle and leaves them.
        width, height = out.size
        if width * height >= DENOISE_MIN_PIXELS:
            out = out.filter(ImageFilter.MedianFilter(size=3))
            report["denoised"] = True
            report["applied"].append("denoise")

        # 3. UPSCALE — below roughly 300 DPI tesseract loses thin strokes. Capped, because
        #    enlarging a genuinely low-resolution scan past a point invents detail.
        long_edge = max(out.size)
        if long_edge < UPSCALE_TARGET_LONG_EDGE:
            factor = min(UPSCALE_TARGET_LONG_EDGE / float(long_edge), UPSCALE_MAX_FACTOR)
            if factor > 1.05:
                out = out.resize(
                    (int(out.size[0] * factor), int(out.size[1] * factor)),
                    resample=Image.LANCZOS)
                report["upscale_factor"] = round(factor, 2)
                report["applied"].append("upscale")
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        return im, report

    report["summary"] = (
        "No conditioning was needed." if not report["applied"] else
        "Applied " + ", ".join(report["applied"]) + " before recognition."
        + (f" Page was rotated {report['deskew_degrees']} degrees to straighten it."
           if report["deskew_degrees"] else ""))
    return out, report


def _reflow(data_dict: dict) -> str:
    lines: dict[tuple, list[str]] = {}
    order: list[tuple] = []
    n = len(data_dict.get("text", []))
    for i in range(n):
        token = (data_dict["text"][i] or "").strip()
        try:
            conf = float(data_dict["conf"][i])
        except (ValueError, KeyError, IndexError):
            conf = -1.0
        if not token or conf < 0:
            continue
        key = (data_dict.get("block_num", [0] * n)[i],
               data_dict.get("par_num", [0] * n)[i],
               data_dict.get("line_num", [0] * n)[i])
        if key not in lines:
            lines[key] = []
            order.append(key)
        lines[key].append(token)
    return "\n".join(" ".join(lines[k]) for k in order)


# ── PDF embedded-image extraction ────────────────────────────────────────────

def rasteriser_available() -> bool:
    """Whether a full vector-PDF rasteriser is importable.

    Detected, not asserted. A status note that states a capability as a fact becomes
    untrue the moment the environment changes, and a hardcoded capability claim is
    exactly what the rest of this product refuses.
    """
    return pdfrender.available()


def rasterise_pdf_pages(pdf_bytes: bytes, only_pages: Optional[set[int]] = None,
                        dpi: int = 200) -> dict[int, bytes]:
    """Render PDF pages to PNG bytes through PDFium. `{}` when unavailable.

    WHY THIS EXISTS. `extract_pdf_page_images()` below only finds rasters somebody already
    embedded. A page whose content is drawn as vectors — which includes a great many
    "scanned-looking" documents and every PDF produced by a report generator — has no
    embedded image to find, so it returned nothing and the page was reported UNREADABLE
    even with a working OCR engine. Rendering the page produces something to read.

    200 dpi because Tesseract's accuracy curve flattens above roughly that for printed
    text, and higher costs time on a demo machine for no recognition gain. Grayscale
    because that is what recognition runs on (`ocr_image` converts to "L" regardless),
    and it is a third of the bytes.

    PDFium is not thread-safe; `pdfrender` serialises every call behind one lock, so
    two uploads OCR-ing at once queue rather than corrupt each other.
    """
    return pdfrender.rasterise(pdf_bytes, only_pages=only_pages, dpi=dpi,
                               grayscale=True)


def extract_pdf_page_images(pdf_bytes: bytes,
                            only_pages: Optional[set[int]] = None) -> dict[int, bytes]:
    """Return {page_index: image_bytes} for pages OCR can attempt.

    Embedded rasters first, because an image somebody actually placed on the page is
    higher fidelity than a re-render of it. Pages with no embedded raster then fall back to
    rasterisation where a rasteriser is installed, so a vector page is no longer reported
    unreadable purely because nobody embedded a picture of it.
    """
    out: dict[int, bytes] = {}
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception:
        # No pypdf, or an unparseable file. Rasterisation may still succeed on its own.
        return rasterise_pdf_pages(pdf_bytes, only_pages)
    for idx, page in enumerate(reader.pages):
        if only_pages is not None and idx not in only_pages:
            continue
        try:
            images = list(page.images)
        except Exception:
            images = []
        if not images:
            continue
        best = None
        best_size = -1
        for im in images:
            try:
                blob = im.data
            except Exception:
                continue
            if blob and len(blob) > best_size:
                best, best_size = blob, len(blob)
        if best:
            out[idx] = best

    # Fall back to rendering for any requested page that yielded no embedded raster.
    wanted = (only_pages if only_pages is not None
              else set(range(len(reader.pages))))
    missing = {p for p in wanted if p not in out}
    if missing:
        out.update(rasterise_pdf_pages(pdf_bytes, missing))
    return out


# ── process-wide provider (lazy, thread-safe) ────────────────────────────────

_provider: Optional[OCRProvider] = None
_provider_lock = threading.Lock()


def default_provider() -> OCRProvider:
    global _provider
    if _provider is None:
        with _provider_lock:
            if _provider is None:
                _provider = TesseractOCRProvider()
    return _provider


def set_provider(provider: Optional[OCRProvider]) -> None:
    global _provider
    with _provider_lock:
        _provider = provider


def available() -> bool:
    if ocr_mode() == OCR_OFF:
        return False
    return default_provider().available()


def status() -> dict:
    p = default_provider()
    mode = ocr_mode()
    runnable = mode != OCR_OFF and p.available()
    # Detection, not assertion: a language is reported only if the engine lists it.
    languages = list(p.languages()) if runnable and hasattr(p, "languages") else []
    return {
        "provider": p.name,
        "local": True,
        "mode": mode,
        "available": runnable,
        "version": p.version() if runnable else None,
        "configured_language": ocr_lang(),
        "languages": languages,
        "chinese_simplified": CHINESE_SIMPLIFIED in languages,
        "supported_inputs": (["image/png", "image/jpeg", "image/tiff", "image/bmp",
                              "image-only PDF (embedded raster)"] if runnable else []),
        "rasteriser": pdfrender.RENDERER if rasteriser_available() else None,
        "vector_pdf_rasterisation": rasteriser_available(),
        "note": (
            "Local Tesseract OCR. Reads direct images and image-only PDFs with "
            "embedded rasters"
            + (", and renders vector PDF pages through PDFium where no raster is "
               "embedded — so a page drawn as vectors is no longer reported unreadable."
               if rasteriser_available() else
               ". Vector-PDF rasterisation needs the PDFium renderer (pypdfium2), which "
               "is not importable here, so a vector page with no embedded raster is "
               "reported unreadable rather than guessed at.")
            if runnable else
            "OCR is not runnable here (tesseract binary or pytesseract/Pillow "
            "missing, or PROVEN_OCR=off). Scanned documents are reported as "
            "unreadable rather than guessed at."
        ),
    }

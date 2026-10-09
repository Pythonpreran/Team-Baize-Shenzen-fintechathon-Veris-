# Open-source components, external services and data sources

VERIS is licensed under the [Apache License 2.0](LICENSE). This file says what every
third-party component, service and data source **does** in the system, and under what
licence or terms — a bare licence list cannot tell a reader which parts of the product
depend on which outside party. The generated licence texts of every installed runtime
package, including transitive dependencies, are in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) (`scripts/gen_third_party_notices.py`).

The rule that shapes every row below: **no machine-learning model makes a consequential
decision.** No model decides whether cover exists, resolves which clause governs,
computes or adjusts a settlement figure, sets an evidence class or produces a score. The
modules that do those things (`resolve.py`, `arithmetic.py`, `grading.py`, `score.py`)
never import the AI layer, and a test fails the build if one ever does.

## Runtime dependencies

Pinned in `requirements.txt` and `pyproject.toml`.

| Component | Licence | Role in VERIS |
|---|---|---|
| **FastAPI** | MIT | The HTTP API: document intake, the Review Brief, consent-scoped disclosure, the case lifecycle, the record export, and the static front end. |
| **Starlette** (via FastAPI) | BSD-3-Clause | The ASGI toolkit underneath FastAPI: routing, cookies, static files and streaming responses. |
| **uvicorn** | BSD-3-Clause | The ASGI server that runs the app. |
| **pydantic** | MIT | Validation of every request body and of the corpus manifest (`corpus.py`), so a malformed manifest entry fails loudly rather than resolving to the wrong document. |
| **PyYAML** | MIT | Loads the clause taxonomy, the rule files and the corpus manifest. Rules are data so a domain reviewer can read them without reading Python. Always `safe_load`. |
| **pypdf** | BSD-3-Clause | Text extraction from uploaded PDFs, page by page. Every quotation and character offset the product cites is measured against pypdf's text, and it also finds embedded page images for OCR. |
| **python-multipart** | Apache-2.0 | Parses multipart upload bodies for `POST /api/bundle`. |
| **pypdfium2** and **PDFium** | Apache-2.0 / BSD-3-Clause (binding); BSD-3-Clause / Apache-2.0 (PDFium) | Renders PDF pages to pixels, in one place (`pdfrender.py`) behind one lock because PDFium is not thread-safe. Two uses: the evidence viewer draws the cited page of a document **the user uploaded** with the quotation highlighted, and the OCR layer renders a page drawn as vectors so Tesseract has pixels to read. It never draws a page of a third party's published document — those are shown as the cited paragraph plus a link to the publisher. Optional: without it the viewer shows the quotation and says the picture is unavailable. It replaced a copyleft-licensed renderer that was incompatible with releasing this repository under Apache-2.0. |
| **openai** (Python SDK) | Apache-2.0 | **Optional.** The client for the one OpenAI-compatible model endpoint, used only when a key is configured: semantic retrieval, model-proposed clause mechanisms that a deterministic verifier must confirm before anything binds, plain-language explanation and presentation wording. Every model output passes the outcome gate; none reaches a figure. |
| **numpy** | BSD-3-Clause (and bundled permissive licences) | **Optional.** Vector arithmetic for embedding retrieval. Without it retrieval falls back to BM25 and says so. |
| **pytesseract** | Apache-2.0 | **Optional.** Wrapper that calls the Tesseract binary for OCR of scanned pages. OCR confidence is extraction metadata, never evidence confidence: a page read poorly caps any finding resting on it at UNESTABLISHED. |
| **Pillow** | MIT-CMU | **Optional, with OCR.** Image decoding and the pre-OCR conditioning (deskew, denoise, upscale). |
| **SQLite** (`sqlite3`, Python standard library) | Public domain | The source / accounting / computation / case / record stores. There is deliberately no breach, violation, deficiency or score column anywhere in the schema. |

Transitive dependencies (anyio, httpx, h11, idna, certifi, typing-extensions, tqdm and the
rest) are listed with their licences and texts in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

### Programs outside Python

| Component | Licence | Role |
|---|---|---|
| **Tesseract OCR** (binary) | Apache-2.0 | **Optional.** The OCR engine pytesseract drives. Not installable by pip; `ocr.status()` reports by detection whether it is present. |
| **Node.js** | MIT | Development only: `node --check` on the front end and `tools/smoke_frontend.mjs`. The front end itself has no build step and no npm dependency. |

## Development and test tools

Not part of a deployment.

| Component | Licence | Role |
|---|---|---|
| **pytest** | MIT | The test runner. The suite runs fully offline: `tests/conftest.py` strips model keys and forces live sources off. |
| **hypothesis** | MPL-2.0 | Property-based tests for the settlement arithmetic (an illustration never exceeds the loss; a null threshold is never silently defaulted). |
| **httpx** | BSD-3-Clause | The transport behind `fastapi.testclient`. (Also a runtime dependency of the openai SDK.) |
| **pip-licenses** | MIT | Generates THIRD_PARTY_NOTICES.md. |

## External services

None is required. With nothing configured the product runs every deterministic path,
and every test passes, with no network access at all.

| Service | When it is called | Role | Terms |
|---|---|---|---|
| **An OpenAI-compatible model API** (default `api.openai.com`) | Only when `OPENAI_API_KEY` is set | Retrieval embeddings, mechanism proposals that must pass verification, explanations and presentation wording. Discarded and replaced by deterministic wording whenever an output fails a gate. | The provider's own terms of service; a commercial service, not open source. The swappable multi-provider layer (with a deterministic `none` profile) is separate, later work. |
| **Singapore government open-data APIs** — data.gov.sg real-time rainfall and 24-hour forecast; the SingStat Table Builder (construction cost index M211251); the ACRA company register via the data.gov.sg datastore; OneMap geocoding | Only when `PROVEN_LIVE_SOURCES=1` | The monitoring and discovery agents and the drift index. Each call has a timeout and falls back to a labelled fixture, and a fixture is never rendered as live. | Each publisher's open-data terms. |
| **An SMTP relay** | Only when `PROVEN_SMTP_HOST` and `PROVEN_SMTP_FROM` are set | Delivers notifications. Unset, every notification is still composed in full and shown in an in-app outbox, marked as not sent. | Whichever relay the deployer configures. |
| **The example business's website** (`demo-site/site.py`, a local process) | Only when discovery runs against it | A stand-in website for the fictional example business, so discovery makes a genuine HTTP request and parse. | Ours; synthetic content. |

**Accounting systems.** A live accounting integration is a **documented seam, not a
built feature.** The "accounting feed" in the business workspace is fixed demonstration
data labelled `simulated: true` everywhere it appears. An earlier build carried a live
connector to one vendor's API; it was removed rather than shipped half-supported. What a
real connector must supply, and what it must never change, is specified in
`connectors.py` (`ACCOUNTING_SEAM`) and recorded as a known gap in
[SECURITY.md](SECURITY.md).

## Data sources

| Source | Status | Role |
|---|---|---|
| **Published insurer policy wordings, brochures and webpages** (Singapore and Malaysia), a statute and a court judgment, listed in [`corpus/manifest.yaml`](corpus/manifest.yaml) | Third-party copyrighted publications. **Not in this repository.** Fetched into a corpus directory outside git by `scripts/fetch_corpus.py`, verified by sha256 against the manifest at fetch time and again when read. | The base wording of the worked example, the cross-carrier clause comparison, and the alignment measurement. The manifest records publisher, source URL, retrieval hash and what each document is *not* authority for. The product shows a cited paragraph of these documents with a link to the publisher's own copy — never their pages. |
| **Synthetic example documents** (`demo-documents/`, `fixtures/`) | Generated by the team with `tests/pdfbuild*.py`; synthetic and labelled synthetic | A fictional business and its paperwork. Any synthetic document that bears a real insurer's name carries a visible `SYNTHETIC SAMPLE — NOT ISSUED BY ANY INSURER` mark on every page. |
| **Market statistics quoted on the business-interruption screen** (`bi.py`) | Third-party published research, quoted as short findings with their sources named | Context for why the screen exists. None feeds a figure. |
| **The construction cost series in `drift.py`** | Synthetic, authored by the team, labelled `DECLARED_SAMPLE_SERIES` | Shaped like a published index so that substituting a real one changes the figures and nothing else in the method. |

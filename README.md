# VERIS

An **evidence-bound insurance agent**. It reads the policies a household or a small
business already holds, answers questions about them with page-cited evidence, maps
where protection is missing or doubled up, and carries a claim from first notice to
settlement — asking before every step that has an effect outside the product.

A deterministic core, not a language model, computes every number. No machine-learning
model decides whether cover exists, resolves which clause governs, computes or adjusts a
settlement figure, or sets an evidence class. The test suite asserts that against the
import graph rather than claiming it in prose.

> This README is a placeholder. The full documentation set — architecture, security
> self-assessment, compliance matrix, evaluation results and deployment guide — lands
> with the documentation task.

## Quickstart

```bash
python -m venv .venv
.venv/Scripts/Activate.ps1        # Windows PowerShell
# source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt   # applies constraints.txt: every package pinned

# Optional: the published insurer wordings the worked example reads. See below.
python scripts/fetch_corpus.py --seed-from ../06-evidence

python -m pytest -q
python -m uvicorn proven.api:app --port 8000
```

Then open <http://localhost:8000>.

`requirements.txt` pins the direct dependencies, and the `constraints.txt` it applies
pins everything they pull in. So a fresh install gets exactly the versions the suite
was run against, and exactly the versions `THIRD_PARTY_NOTICES.md` lists.

## One-time local demonstration migration

Older local portfolio stores can contain SME demonstration bundles created before VERIS
persisted a server-stamped bundle origin. Their public fields are intentionally similar
to an ordinary uploaded analysis, so VERIS does not infer the origin, relabel a record,
or remove a duplicate. It leaves an unmarked legacy bundle unchanged and tells the owner
to start a new local portfolio and reload the synthetic household. This conservative
one-time path protects uploaded records; current demonstration and uploaded bundles keep
their durable origin on both the business and policy records.

## The document corpus

The engine reads real, published policy wordings, statute text and one judgment. Those
are **not in this repository**. They are other people's copyrighted publications: this
project has a reader's right to them, not a publisher's.

What *is* committed is [`corpus/manifest.yaml`](corpus/manifest.yaml) — for every
document: its publisher, the URL it was retrieved from, its country and document class,
the sha256 of the exact bytes, and what the document is *not* authority for. So any
figure the product quotes can be re-fetched and re-checked at source by someone who has
only this repository.

```bash
python scripts/fetch_corpus.py --seed-from <dir>   # from local copies (primary path)
python scripts/fetch_corpus.py                      # download from the publishers
python scripts/fetch_corpus.py --status             # what the corpus holds
```

Without the corpus the product still runs: the synthetic examples load, every
deterministic path works, and the tests that genuinely need a published wording **skip
with a reason naming the missing document** rather than passing quietly with fewer
assertions.

The synthetic example documents in `demo-documents/` *are* committed, because we
generated them. One document is deliberately absent from that directory: the example's
base policy wording, which is a real insurer's publication. The server attaches it from
the corpus, and there is no route that serves it to a browser.

## Repository hygiene

```bash
python scripts/scan_repo.py      # secrets, forbidden files, forbidden strings
node --check web/app.js          # the front end has no build step
```

`scan_repo.py` fails on key-shaped literals, on any `.env` or database file, and on a
third-party PDF anywhere outside the synthetic set. It runs in CI.

## Layout

| Path | What is in it |
|---|---|
| `src/proven/` | the engine: ingestion, clause accounting, extraction, rules, arithmetic, evidence grading, abstention gates |
| `taxonomy/` | the clause taxonomy and mechanism definitions, as data a domain reviewer can read |
| `corpus/` | the manifest of third-party documents (the documents themselves live outside git) |
| `web/` | front end. Vanilla HTML, CSS and JS. No build step, no npm dependency |
| `tests/` | the suite, which must run fully offline |
| `eval/` | the measurement harness and its labels, with each label's derivation recorded |
| `scripts/` | corpus fetcher, repository scanner, and the THIRD_PARTY_NOTICES generator |
| `demo-documents/` | the synthetic documents of the worked example |
| `fixtures/` | structured fixtures for documents a real business would supply |

## Licence

[Apache License 2.0](LICENSE) — see also [NOTICE](NOTICE). Every third-party component,
external service and data source, with its role and licence, is in
[OPEN-SOURCE.md](OPEN-SOURCE.md); generated licence texts are in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). To report a vulnerability, see
[SECURITY.md](SECURITY.md).

The published documents in the corpus are not covered by this licence and are not in the
repository. The evidence viewer shows a cited paragraph of such a document with a link to
the publisher's own copy; full pages are drawn only for documents you uploaded yourself.

## Team

Preran S Gowda, Shamanth M S, Shreya Belavatha Jayaram and Shashank S.

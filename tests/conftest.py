import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# ── TEST ISOLATION — the normal suite must NEVER touch the network ───────────
#
# `PROVEN_LIVE_SOURCES` flips Sentinel, Discovery and the drift index onto real
# outbound government-API calls. If it leaks in from the shell (it did once, turning a
# 90s offline run into a 9-minute run with failures), the deterministic suite would
# silently depend on the internet, API keys and external services.
#
# Force it OFF for the entire test process here, before any `proven` module is imported,
# so `python -m pytest -q` is deterministic and offline no matter what the environment
# holds. `config.live_sources_enabled()` reads this at call time, so overwriting it here
# is authoritative. The live paths are still fully exercised — by MOCKING the HTTP layer
# in test_live_sources.py, never by a real call — so nothing is weakened.
#
# A genuine live integration run is opt-in and lives OUTSIDE this suite: set the flag in
# the shell and run the app (see DEMO-SCRIPT.md / .env.example). It is deliberately not
# collected by the default `pytest` invocation.
os.environ["PROVEN_LIVE_SOURCES"] = "0"

# ── THE SAME RULE FOR MODEL KEYS ─────────────────────────────────────────────
#
# The flag above stops government API calls. It does not stop a model call, and model
# calls have leaked into the suite by accident: once `discovery.discover_business()` began
# asking a model to read each fetched page, all sixteen pre-existing discovery tests would
# have started making three paid HTTP requests each — on a machine where OPENAI_API_KEY is
# set at Machine scope, so every developer would inherit it silently.
#
# The damage is not mainly the cost. It is that assertions about fact counts would depend
# on what a non-deterministic model happened to return that morning, and the suite would
# fail on a train. So the keys are removed from the process before any `proven` module is
# imported. Tests that need the model path exercise it by monkeypatching the transport and
# asserting on the gates, which is where the risk lives.
#
# Every `*_API_KEY`, not a list of names. A list has to be kept in step with every provider
# the model layer learns to read, and the failure mode of forgetting one is the silent
# paid-call leak described above. Stripping the whole family costs nothing: no test needs a
# real key, because none is allowed to make a real call.
def _model_key_names() -> list[str]:
    return sorted({"OPENAI_API_KEY"} | {k for k in os.environ if k.upper().endswith("_API_KEY")})


for _model_key_env in _model_key_names():
    os.environ.pop(_model_key_env, None)

import pytest  # noqa: E402

from proven import corpus  # noqa: E402


@pytest.fixture(autouse=True)
def _force_offline_sources(monkeypatch):
    """Belt-and-braces per test: even if a test sets the flag, it is reset around it."""
    monkeypatch.setenv("PROVEN_LIVE_SOURCES", "0")
    for env in _model_key_names():
        monkeypatch.delenv(env, raising=False)
    yield


# ── THE CORPUS IS OPTIONAL, AND ITS ABSENCE IS VISIBLE ───────────────────────
#
# The published insurer wordings the engine was measured against are third-party
# copyrighted material. They are not in git; they live in the corpus directory, fetched
# by `scripts/fetch_corpus.py`. A clean clone therefore has the code and not the
# documents, and the suite has to behave correctly in both states.
#
# The wrong way to handle that is a bare `if not path.exists(): return` inside each
# test, because a test that silently asserts nothing passes, and the run looks identical
# whether the corpus was read or not. So the dependency is declared with
# `@pytest.mark.requires_corpus(...)` and turned into a SKIP that names the missing
# document. `pytest -q` shows the skip count; CI prints the reasons.

def pytest_collection_modifyitems(config, items):
    for item in items:
        marker = item.get_closest_marker("requires_corpus")
        if marker is None:
            continue
        refs = tuple(marker.args)
        try:
            absent = corpus.missing(refs) if refs else corpus.missing()
        except corpus.ManifestError as exc:
            item.add_marker(pytest.mark.skip(
                reason=f"corpus manifest could not be read: {exc}"))
            continue
        if absent:
            item.add_marker(pytest.mark.skip(reason=_FETCH_HINT.format(
                what=", ".join(absent))))


_FETCH_HINT = ("corpus document(s) not fetched: {what}. Run "
               "`python scripts/fetch_corpus.py --seed-from ../06-evidence`")


# ── and the same rule for the WORKED EXAMPLE, which is not one document ──────
#
# The worked example is assembled from the published 28-page wording plus the synthetic
# fixtures, and `pipeline.run_case()` is reached from roughly two hundred tests — API
# routes, role boundaries, the AI boundary, the arithmetic, the record chain. Declaring
# `requires_corpus` on every module that eventually touches it would mean marking twenty
# files, and marking a whole file skips the tests in it that need nothing of the kind.
#
# So the dependency is caught where it is actually raised. `run_case()` raises the typed
# `corpus.CorpusMissing`; this hook turns an uncaught one into a SKIP naming the missing
# document. Narrow on purpose:
#
#   * only our own `CorpusMissing`, never a generic error — a real bug must still fail
#   * plus an OSError whose filename sits INSIDE the corpus directory, which is the same
#     condition reached one layer lower when a test opens a corpus path itself
#   * and ONLY when the document is genuinely absent. On a seeded machine — the normal
#     developer and CI state — a `CorpusMissing` is by definition a bug: a mis-resolved
#     id, a wrong corpus directory, a regression in `original_path()`. Converting that
#     into a skip would turn roughly two hundred tests green-by-absence and still exit 0,
#     which is the exact failure this whole mechanism exists to prevent.
#
# Everything else fails as it should.

def _corpus_absence(exc: BaseException) -> str | None:
    if isinstance(exc, corpus.CorpusMissing):
        return str(exc) if _really_absent(exc) else None
    filename = getattr(exc, "filename", None)
    if isinstance(exc, OSError) and filename:
        try:
            path = Path(str(filename)).resolve()
            root = corpus.corpus_dir().resolve()
        except OSError:                              # unresolvable path: not our case
            return None
        if root in path.parents and not path.exists():
            return _FETCH_HINT.format(what=path.name)
    return None


def _really_absent(exc: corpus.CorpusMissing) -> bool:
    """Is the document the exception names actually missing from the corpus?"""
    doc_id = getattr(exc, "document_id", None)
    try:
        return bool(corpus.missing([doc_id]) if doc_id else corpus.missing())
    except corpus.CorpusError:
        return True          # the manifest itself is unreadable: trust the exception


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.outcome != "failed" or call.excinfo is None:
        return
    reason = _corpus_absence(call.excinfo.value)
    if reason is None:
        return
    report.outcome = "skipped"
    # pytest renders a 3-tuple longrepr as the skip location and reason, which is what
    # puts the document name in the `-rs` summary instead of a traceback.
    report.longrepr = (str(item.path), item.location[1] or 0, f"Skipped: {reason}")

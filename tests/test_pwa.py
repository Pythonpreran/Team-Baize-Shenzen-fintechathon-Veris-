"""T25 installable shell and camera capture.

The service worker may keep the public application shell and nothing else. These tests
pin that from three sides: what the server serves (and to whom in hosted mode), what the
worker script is allowed to store, and what a camera photo becomes on its way through the
ordinary upload pipeline.
"""
from __future__ import annotations

import base64
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from proven import api, auth, persistence, sandbox, security

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
SW = (WEB / "sw.js").read_text(encoding="utf-8")
JS = (WEB / "app.js").read_text(encoding="utf-8")


def _shell_assets() -> list[str]:
    block = SW[SW.index("const SHELL_ASSETS"):SW.index("]);", SW.index("const SHELL_ASSETS"))]
    assets = re.findall(r'"(/[^"]+)"', block)
    if "OFFLINE_PAGE" in block:
        assets.append(re.search(r'const OFFLINE_PAGE = "([^"]+)"', SW).group(1))
    return assets


@pytest.fixture()
def client():
    api.SESSIONS.clear()
    with TestClient(api.app) as instance:
        yield instance
    api.SESSIONS.clear()


def test_the_worker_is_served_from_the_root_as_script_under_the_csp(client):
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["cache-control"] == "no-cache"
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.content == (WEB / "sw.js").read_bytes()


def test_the_manifest_describes_an_installable_app_whose_icons_exist(client):
    response = client.get("/static/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = response.json()
    assert manifest["start_url"] == "/app" and manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    sizes = set()
    for icon in manifest["icons"]:
        served = client.get(icon["src"])
        assert served.status_code == 200, icon["src"]
        assert served.headers["content-type"].startswith(icon["type"])
        sizes.add(icon["sizes"])
    assert {"192x192", "512x512"} <= sizes
    index = (WEB / "index.html").read_text(encoding="utf-8")
    assert '<link rel="manifest" href="/static/manifest.webmanifest">' in index


def test_every_precached_shell_file_is_a_public_static_file_that_exists(client):
    assets = _shell_assets()
    assert assets, "the worker declares no shell list"
    for path in assets:
        assert path.startswith("/static/"), f"{path} is outside the public static tree"
        assert not path.startswith("/static/landing/")
        assert client.get(path).status_code == 200, f"{path} would fail the worker install"


def test_the_worker_never_stores_api_responses_navigations_or_changes():
    fetch_handler = SW[SW.index('self.addEventListener("fetch"'):]
    # Order matters: each guard returns before respondWith can be reached.
    method = fetch_handler.index('request.method !== "GET"')
    origin = fetch_handler.index("url.origin !== self.location.origin")
    api_guard = fetch_handler.index('url.pathname.startsWith("/api/")')
    navigate = fetch_handler.index('request.mode === "navigate"')
    assert method < origin < api_guard < navigate
    assert "offlineNavigation(request)" in fetch_handler
    offline = SW[SW.index("async function offlineNavigation"):SW.index('self.addEventListener("install"')]
    assert "cache.put" not in offline and ".put(" not in offline, "a navigation must never be stored"
    network_first = SW[SW.index("async function networkFirst"):SW.index("async function offlineNavigation")]
    assert 'response.type === "basic"' in network_first and "response.ok" in network_first
    assert "cache.put(url.pathname" in network_first
    cacheable = SW[SW.index("function cacheable"):SW.index("async function clearShellCaches")]
    for guard in ('request.method === "GET"', '!url.pathname.startsWith("/api/")',
                  'request.mode !== "navigate"', "!url.search", "SHELL_PATHS.has(url.pathname)"):
        assert guard in cacheable


def test_signing_out_clears_the_shell_caches_and_registration_is_feature_detected():
    logout = JS[JS.index("async function logout"):JS.index("window.logout = logout")]
    assert "await clearShellCaches();" in logout
    assert logout.index("/api/auth/logout") < logout.index("clearShellCaches")
    register = JS[JS.index("function registerShellWorker"):JS.index("async function clearShellCaches")]
    assert '"serviceWorker" in nav' in register
    assert 'register("/sw.js", { scope: "/" })' in register
    clear = JS[JS.index("async function clearShellCaches"):JS.index("registerShellWorker();")]
    assert "veris:clear-caches" in clear and "caches.delete" in clear
    assert '"veris:clear-caches"' in SW


@pytest.fixture()
def hosted_client(tmp_path, monkeypatch):
    monkeypatch.setenv("VERIS_MODE", "hosted")
    monkeypatch.setenv(auth.HOSTED_AUTH_DB_ENV, str(tmp_path / "hosted-auth.sqlite"))
    monkeypatch.setenv(auth.HOSTED_INVITES_ENV, json.dumps({"judge-invite": "SME"}))
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(tmp_path / "portfolio.sqlite"))
    monkeypatch.setenv(persistence.MASTER_KEY_ENV, base64.urlsafe_b64encode(b"p" * 32).decode("ascii"))
    monkeypatch.setenv(security.RECORD_HMAC_KEY_ENV, "test-only-record-integrity-material-32-bytes")
    monkeypatch.setenv(sandbox.SANDBOX_DB_ENV, str(tmp_path / "sandbox.sqlite"))
    monkeypatch.setenv(sandbox.SANDBOX_LOG_ENV, str(tmp_path / "sandbox-operations.jsonl"))
    monkeypatch.setenv(sandbox.SANDBOX_KEY_ENV, base64.urlsafe_b64encode(b"s" * 32).decode("ascii"))
    api.SESSIONS.clear()
    auth.LOGIN_RATE_LIMITER.clear()
    with TestClient(api.app, base_url="https://testserver") as instance:
        yield instance
    api.SESSIONS.clear()
    auth.LOGIN_RATE_LIMITER.clear()


def test_hosted_mode_serves_the_shell_publicly_but_never_the_workspace(hosted_client):
    for path in ("/sw.js", "/static/manifest.webmanifest", "/static/offline.html",
                 "/static/icons/veris-192.png"):
        assert hosted_client.get(path).status_code == 200, path
    assert hosted_client.get("/app").status_code == 401
    assert hosted_client.get("/api/portfolio").status_code == 401


def test_the_committed_icons_are_the_generators_output():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import gen_pwa_icons
    finally:
        sys.path.pop(0)
    assert (WEB / "icons" / "veris-192.png").read_bytes() == gen_pwa_icons.render(192)
    large = (WEB / "icons" / "veris-512.png").read_bytes()
    assert large.startswith(b"\x89PNG\r\n\x1a\n")
    assert int.from_bytes(large[16:20], "big") == 512 and int.from_bytes(large[20:24], "big") == 512


def test_camera_inputs_use_the_native_capture_control_on_both_upload_paths():
    assert 'id="camera-input" accept="image/*" capture="environment"' in JS
    assert 'id="claim-camera" type="file" accept="image/*" capture="environment"' in JS
    upload = JS[JS.index("async function claimUpload"):JS.index("async function claimBuildPacket")]
    assert '$("#claim-camera")' in upload and "claimFileProblem" in upload
    # The browser-side limit is the server's limit, not a second policy.
    assert "const CLAIM_UPLOAD_MAX_BYTES = 2_000_000;" in JS
    assert "blob = await upload.read(2_000_001)" in (ROOT / "src" / "proven" / "api.py").read_text(encoding="utf-8")


def test_no_model_payload_can_carry_an_image():
    """Document images never reach a provider: the model layer sends text messages only."""
    source = "\n".join(path.read_text(encoding="utf-8")
                       for path in (ROOT / "src" / "proven").rglob("*.py"))
    for needle in ("image_url", "input_image", '"type": "image"'):
        assert needle not in source, f"{needle} would let an image reach a model provider"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_camera_photo_becomes_a_pdf_the_upload_pipeline_reads_and_reports(tmp_path, client):
    from PIL import Image
    import pypdf

    photo = tmp_path / "photo.jpg"
    image = Image.new("RGB", (320, 240), (240, 236, 228))
    for x in range(40, 280):
        for y in range(100, 110):
            image.putpixel((x, y), (20, 20, 20))
    image.save(photo, "JPEG", quality=85)
    output = tmp_path / "photo.pdf"
    proc = subprocess.run(
        [shutil.which("node"), str(ROOT / "tools" / "smoke_frontend.mjs"), "--photo-pdf",
         str(photo), str(output), "320", "240"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = output.read_bytes()

    reader = pypdf.PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 1
    image_object = reader.pages[0]["/Resources"]["/XObject"]["/Im0"].get_object()
    assert image_object["/Filter"] == "/DCTDecode"
    assert (image_object["/Width"], image_object["/Height"]) == (320, 240)
    assert image_object.get_data() == photo.read_bytes()

    # The ordinary pipeline: magic bytes, size cap, parser. A photograph has no text layer,
    # and the build reports that plainly instead of guessing at what the picture says.
    response = client.post("/api/bundle", files=[("files", ("photo-test.pdf", data, "application/pdf"))])
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "BUNDLE_INVALID"
    assert json.loads(error["detail"])[0]["code"] == "NO_TEXT_LAYER"

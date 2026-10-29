"""Static guardrails for the WebUI extension; browser usability is checked by hand."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "web/control-surface"


def test_extension_manifest_and_inert_rendering():
    [entry] = json.loads((ROOT / "extensions.json").read_text())["extensions"]
    assert entry["id"] == "quoth"
    assert entry["sidecar"] == {"type": "loopback", "origin": "http://127.0.0.1:17889",
                                "health_path": "/health", "proxy_auth": "token-v1"}
    script = (ROOT / "quoth/boot.js").read_text()
    style = (ROOT / "quoth/control.css").read_text()
    assert script.count("'Pipeline'") >= 1
    for name in ("Snapshots & freshness", "Playground", "Quality"):
        assert name in script
    assert "textContent" in script and "innerHTML" not in script
    assert "localStorage" not in script and "sessionStorage" not in script
    assert "/api/extensions/quoth/sidecar/api/memory/v1" in script
    assert "prefers-reduced-motion" in style and "max-width:767px" in style
    assert "aria-modal" in script and "Escape" in script and "restoreFocus" in script


def test_no_network_assets_and_a_size_budget():
    """The WebUI CSP may refuse remote fonts, scripts and images: everything ships inline."""
    script = (ROOT / "quoth/boot.js").read_text()
    style = (ROOT / "quoth/control.css").read_text()
    # The only URL is the SVG namespace, which is never fetched.
    assert re.findall(r"https?://[^'\"\s)]+", script) == ["http://www.w3.org/2000/svg"]
    assert not re.search(r"https?://|@import|@font-face|url\(", style)
    assert "createElementNS" in script and "<svg" not in script
    assert "console." not in script  # no memory text in logs
    # Light and dark follow the WebUI's `.dark` on :root (via data-scheme); the media query is the
    # fallback only when no scheme was set; the WebUI's real variables tint and edge the panel.
    assert "contains('dark')" in script and "getPropertyValue('--bg')" in script
    assert '[data-scheme="dark"]' in style and ":not([data-scheme])" in style and "prefers-color-scheme: dark" in style
    for name in ("--bg", "--surface", "--border", "--border2"):
        assert f"var({name}," in style
    for retired in ("--bg-primary", "--text-primary", "--text-secondary", "data-theme"):
        assert retired not in style
    assert len(script.encode()) + len(style.encode()) < 160_000


def test_preview_harness_is_not_shipped():
    """The synthetic preview lives under tests/, outside the extension directory the wheel ships."""
    assert not (ROOT / "preview").exists()
    assert {p.name for p in (ROOT / "quoth").iterdir()} == {"boot.js", "control.css"}

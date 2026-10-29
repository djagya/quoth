"""quoth models: pinned downloads, sha256 checks, resume, and the CLI's exit codes."""
import hashlib
import json

import pytest

from quoth import models
from quoth.cli import main


def pin_for(files: dict[str, bytes]) -> models.Pin:
    return models.Pin("toy", "org/toy", "abc123",
                      tuple((name, hashlib.sha256(data).hexdigest()) for name, data in files.items()))


def serve(monkeypatch, files: dict[str, bytes], calls: list):
    def fake(url, part):
        calls.append(url)
        name = url.split("/resolve/abc123/", 1)[1]
        part.write_bytes(files[name])
    monkeypatch.setattr(models, "_download", fake)


def test_fetch_downloads_verifies_and_skips_what_is_already_there(tmp_path, monkeypatch):
    files = {"config.json": b"{}", "onnx/model.onnx": b"weights"}
    pin, calls = pin_for(files), []
    serve(monkeypatch, files, calls)
    root = models.fetch(pin, tmp_path)
    assert root == tmp_path / "org/toy"
    assert (root / "onnx/model.onnx").read_bytes() == b"weights"
    assert calls == ["https://huggingface.co/org/toy/resolve/abc123/config.json",
                     "https://huggingface.co/org/toy/resolve/abc123/onnx/model.onnx"]
    assert models.verify(pin, tmp_path) == {"config.json": "ok", "onnx/model.onnx": "ok"}
    models.fetch(pin, tmp_path)
    assert len(calls) == 2
    assert not list(root.rglob("*.part"))


def test_a_mismatching_download_is_discarded(tmp_path, monkeypatch):
    pin = pin_for({"config.json": b"{}"})
    serve(monkeypatch, {"config.json": b"tampered"}, [])
    with pytest.raises(models.ModelError, match="does not match the pin"):
        models.fetch(pin, tmp_path)
    assert models.verify(pin, tmp_path) == {"config.json": "missing"}
    assert not (tmp_path / "org/toy/config.json.part").exists()


def test_verify_reports_a_changed_file(tmp_path):
    pin = pin_for({"config.json": b"{}"})
    (tmp_path / "org/toy").mkdir(parents=True)
    (tmp_path / "org/toy/config.json").write_bytes(b"changed")
    assert models.verify(pin, tmp_path) == {"config.json": "mismatch"}


def test_the_embedder_pin_comes_from_the_models_manifest():
    pin = models.embedder_pin()
    assert pin.repo == "BAAI/bge-m3" and len(pin.revision) == 40
    assert {f for f, _ in pin.files} >= {"onnx/model.onnx", "onnx/model.onnx_data", "tokenizer.json"}


def test_reranker_pins_name_only_onnx_models():
    assert models.reranker_pin("bge-reranker-v2-m3-int8").repo == "onnx-community/bge-reranker-v2-m3-ONNX"
    with pytest.raises(models.ModelError, match="unknown reranker"):
        models.reranker_pin("hash-rerank")


def test_cli_verify_exits_1_until_fetch_fills_the_cache(tmp_path, monkeypatch, capsys):
    from quoth import rerank
    spec = rerank.MODELS["bge-reranker-v2-m3-int8"]
    blobs = {f: f"blob {f}".encode() for f in spec.files}
    monkeypatch.setattr(models, "reranker_pin", lambda name: pin_for(blobs))
    serve(monkeypatch, blobs, [])
    args = ["--cache", str(tmp_path), "--no-embedder", "--reranker", "bge-reranker-v2-m3-int8"]
    assert main(["models", "verify", *args]) == 1
    capsys.readouterr()
    assert main(["models", "fetch", *args]) == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report["toy"]["files"].values()) == {"ok"}
    assert main(["models", "verify", *args]) == 0
    assert main(["models", "fetch", "--cache", str(tmp_path), "--no-embedder"]) == 1

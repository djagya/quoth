"""Download and verify the local ONNX model files.

quoth never downloads a model while it runs: embedding and reranking load only files that
are already in the model cache (``QUOTH_MODEL_CACHE``, else ``$QUOTH_HOME/models``). This
module is the one explicit step that fills that cache. Every file is fetched from the
Hugging Face hub at a pinned revision and kept only if its SHA-256 matches the pin.

- ``bge-m3``: the embedder, pinned by ``eval/models-manifest.json`` (about 2.3 GB).
- reranker names (``quoth.rerank.MODELS``): the prefetch gate's cross-encoder.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .assets import eval_path

__all__ = ["Pin", "ModelError", "embedder_pin", "reranker_pin", "fetch", "verify", "main"]

HUB = "https://huggingface.co"
CHUNK = 1 << 20


class ModelError(RuntimeError):
    pass


@dataclass(frozen=True)
class Pin:
    name: str
    repo: str
    revision: str
    files: tuple[tuple[str, str], ...]  # (path in repo, sha256)

    def directory(self, cache: Path | None = None) -> Path:
        return (cache or paths.model_cache()) / self.repo


def embedder_pin() -> Pin:
    manifest = json.loads(eval_path("models-manifest.json").read_text(encoding="utf-8"))
    return Pin("bge-m3", manifest["model"], manifest["revision"],
               tuple((f["file"], f["sha256"]) for f in manifest["files"]))


def reranker_pin(name: str) -> Pin:
    from .rerank import MODELS
    spec = MODELS.get(name)
    if spec is None or spec.backend != "onnx":
        onnx = sorted(n for n, s in MODELS.items() if s.backend == "onnx")
        raise ModelError(f"unknown reranker {name!r}; expected one of {onnx}")
    return Pin(name, spec.repo, spec.revision, tuple(spec.sha256))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def verify(pin: Pin, cache: Path | None = None) -> dict[str, str]:
    """{file: "ok" | "missing" | "mismatch"} for every pinned file."""
    root = pin.directory(cache)
    out = {}
    for name, digest in pin.files:
        path = root / name
        out[name] = "missing" if not path.is_file() else ("ok" if _sha256(path) == digest else "mismatch")
    return out


def _download(url: str, part: Path) -> None:
    """Fetch ``url`` into ``part``, resuming a partial file with an HTTP range request."""
    have = part.stat().st_size if part.exists() else 0
    request = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            mode = "ab" if have and response.status == 206 else "wb"
            with open(part, mode) as f:
                while block := response.read(CHUNK):
                    f.write(block)
    except urllib.error.HTTPError as e:
        if e.code == 416:  # the partial file is already complete
            return
        raise ModelError(f"download failed ({e.code}): {url}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise ModelError(f"download failed ({type(e).__name__}): {url}") from None


def fetch(pin: Pin, cache: Path | None = None, *, log=None) -> Path:
    """Download every missing or mismatching file of ``pin`` and verify it."""
    root = pin.directory(cache)
    for name, digest in pin.files:
        path = root / name
        if path.is_file() and _sha256(path) == digest:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        if log:
            log(f"fetching {pin.repo}/{name}")
        _download(f"{HUB}/{pin.repo}/resolve/{pin.revision}/{name}", part)
        if _sha256(part) != digest:
            part.unlink()
            raise ModelError(f"{pin.repo}/{name}: sha256 does not match the pin")
        os.replace(part, path)
    return root


def _pins(a) -> list[Pin]:
    pins = [] if a.no_embedder else [embedder_pin()]
    pins += [reranker_pin(name) for name in a.reranker or ()]
    if not pins:
        raise ModelError("nothing selected")
    return pins


def main(argv=None) -> int:
    from .rerank import DEFAULT_MODEL
    p = argparse.ArgumentParser(prog="quoth models",
                                description="download and verify the local ONNX model files")
    sub = p.add_subparsers(dest="cmd", required=True)
    for cmd, text in (("fetch", "download missing files at their pinned revision and verify them"),
                      ("verify", "check every pinned file is present with its sha256 (exit 1 if not)")):
        s = sub.add_parser(cmd, help=text)
        s.add_argument("--cache", type=Path, default=None,
                       help="model cache (default $QUOTH_MODEL_CACHE, else $QUOTH_HOME/models)")
        s.add_argument("--reranker", action="append", metavar="NAME",
                       help=f"also this reranker, e.g. {DEFAULT_MODEL}; repeatable")
        s.add_argument("--no-embedder", action="store_true", help="skip the bge-m3 embedder")
    a = p.parse_args(argv)
    try:
        pins = _pins(a)
        if a.cmd == "fetch":
            for pin in pins:
                fetch(pin, a.cache, log=lambda line: print(line, file=sys.stderr))
        report = {pin.name: {"directory": str(pin.directory(a.cache)), "files": verify(pin, a.cache)}
                  for pin in pins}
    except (ModelError, OSError) as e:
        print(f"quoth models: {e}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0 if all(v == "ok" for r in report.values() for v in r["files"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())

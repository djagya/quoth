"""`quoth hermes-plugin …`: install the Hermes "Quoth" memory provider from this release.

The plugin ships inside the wheel so the provider and the sidecar always come from the
same pinned version. `install` copies it to `<hermes-home>/plugins/quoth/` through a
staging directory and an atomic rename; an existing install is replaced only with --force.

The plugin's skills (memory-review, memory-caller) come with it, under
`<hermes-home>/plugins/quoth/skills/<name>/SKILL.md`. Hermes loads them when that
directory is listed in `skills.external_dirs`: external skills are indexed in the system
prompt like local ones, stay read-only (the curator and skill edits never touch them) and
lose a name collision to a local skill of the same name. Upgrading the plugin upgrades them.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

from . import __version__
from .assets import hermes_plugin_dir, webui_extension_dir

FILES = ("__init__.py", "client.py", "plugin.yaml", "README.md")
SKILLS = ("memory-review", "memory-caller")
MARKER = "INSTALLED_FROM.json"


def install(hermes_home: str | Path, *, force: bool = False) -> dict:
    source = hermes_plugin_dir()
    missing = [f for f in FILES if not (source / f).is_file()]
    missing += [f"skills/{s}/SKILL.md" for s in SKILLS if not (source / "skills" / s / "SKILL.md").is_file()]
    if missing:
        raise FileNotFoundError(f"plugin files missing from this install: {missing}")
    plugins = Path(hermes_home) / "plugins"
    target = plugins / "quoth"
    if target.exists() and not force:
        raise FileExistsError(f"{target} exists; pass --force to replace it")
    plugins.mkdir(parents=True, exist_ok=True)
    staging = plugins / f".quoth-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        for name in FILES:
            shutil.copy2(source / name, staging / name)
        for skill in SKILLS:
            (staging / "skills" / skill).mkdir(parents=True)
            shutil.copy2(source / "skills" / skill / "SKILL.md", staging / "skills" / skill / "SKILL.md")
        (staging / MARKER).write_text(json.dumps(
            {"package": "quoth", "version": __version__, "skills": list(SKILLS)},
            indent=2) + "\n", encoding="utf-8")
        old = None
        if target.exists():
            old = plugins / f".quoth-old-{uuid.uuid4().hex}"
            os.rename(target, old)
        os.rename(staging, target)
        if old is not None:
            shutil.rmtree(old)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"installed": str(target), "version": __version__, "skills_dir": str(target / "skills"),
            "skills": list(SKILLS),
            "next": f"list {target / 'skills'} in the Hermes config skills.external_dirs (once)"}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="quoth hermes-plugin", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("install", help="copy the plugin and its skills into <hermes-home>/plugins/quoth/")
    pi.add_argument("--hermes-home", required=True)
    pi.add_argument("--force", action="store_true")
    pp = sub.add_parser("path", help="print the packaged plugin directory")
    what = pp.add_mutually_exclusive_group()
    what.add_argument("--skills", action="store_true", help="the packaged skills directory instead")
    what.add_argument("--webui-extension", action="store_true",
                      help="the packaged WebUI extension directory (extensions.json + quoth/) instead")
    args = p.parse_args(argv)
    if args.cmd == "path":
        if args.webui_extension:
            print(webui_extension_dir())
        else:
            print(hermes_plugin_dir() / "skills" if args.skills else hermes_plugin_dir())
        return 0
    try:
        print(json.dumps(install(args.hermes_home, force=args.force)))
    except (FileExistsError, FileNotFoundError) as exc:
        print(f"quoth hermes-plugin: {exc}", file=sys.stderr)
        return 1
    return 0

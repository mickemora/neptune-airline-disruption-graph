#!/usr/bin/env python3
"""Build the AgentCore Runtime code package: build/agent_runtime.zip.

AgentCore direct code deployment runs on Linux arm64, so dependencies are installed as
manylinux aarch64 wheels for Python 3.13 (works from macOS, Windows or x86 Linux; no Docker).
Zip layout, per the AgentCore docs: dependencies and main.py at the root, files 644 and
directories 755, no __pycache__.

Dependencies are cached in build/agent_deps keyed by a hash of the requirements file, so
rebuilding after an agent code change only re-zips (a few seconds).

  python scripts/build_agent_package.py            # build if needed
  python scripts/build_agent_package.py --force    # reinstall dependencies too
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "agent"
REQUIREMENTS = AGENT / "requirements-runtime.txt"
BUILD = ROOT / "build"
DEPS = BUILD / "agent_deps"
ZIP_PATH = BUILD / "agent_runtime.zip"
PYTHON_VERSION = "3.13"
# Agent modules the runtime needs (chat.py/tools.py are local-only; they stay out).
AGENT_FILES = ["__init__.py", "prompt.py"]


def _requirements_hash() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes() + PYTHON_VERSION.encode()).hexdigest()[:16]


def install_dependencies(force: bool = False) -> None:
    stamp = DEPS / ".requirements-hash"
    if not force and stamp.exists() and stamp.read_text() == _requirements_hash():
        return
    shutil.rmtree(DEPS, ignore_errors=True)
    DEPS.mkdir(parents=True)
    print(f"Installing agent runtime dependencies for linux/arm64, Python {PYTHON_VERSION} ...")
    subprocess.run([
        sys.executable, "-m", "pip", "install", "--quiet",
        "--platform", "manylinux2014_aarch64",
        "--implementation", "cp",
        "--python-version", PYTHON_VERSION,
        "--only-binary=:all:",
        "--target", str(DEPS),
        "-r", str(REQUIREMENTS),
    ], check=True)
    stamp.write_text(_requirements_hash())


def _add(zf: zipfile.ZipFile, path: Path, arcname: str) -> None:
    info = zipfile.ZipInfo(arcname)           # fixed 1980 timestamp: same bytes, same asset hash
    executable = path.suffix == ".so" or ".so." in path.name or "/bin/" in f"/{arcname}"
    info.external_attr = ((0o755 if executable else 0o644) << 16)
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, path.read_bytes())


def build_zip() -> Path:
    tmp = ZIP_PATH.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(DEPS.rglob("*")):
            rel = path.relative_to(DEPS)
            if path.is_dir() or "__pycache__" in rel.parts or rel.name == ".requirements-hash":
                continue
            _add(zf, path, rel.as_posix())
        for name in AGENT_FILES:
            _add(zf, AGENT / name, f"agent/{name}")
        _add(zf, AGENT / "runtime_app.py", "main.py")
    tmp.replace(ZIP_PATH)
    size_mb = ZIP_PATH.stat().st_size / 1e6
    if size_mb > 250:
        raise SystemExit(f"{ZIP_PATH} is {size_mb:.0f} MB; AgentCore's code limit is 250 MB")
    print(f"Built {ZIP_PATH} ({size_mb:.1f} MB)")
    return ZIP_PATH


def build(force: bool = False) -> Path:
    install_dependencies(force)
    return build_zip()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="reinstall dependencies")
    build(parser.parse_args().force)

#!/usr/bin/env python3
"""Rebuild Nutcracker's unminified local noVNC bundle from pinned upstream source.

This is an optional development tool. Running Nutcracker needs neither esbuild
nor Node.js. Install the free esbuild 0.28.2 CLI to rebuild, then run this script.
An existing downloaded archive can be supplied with --source-archive.
"""

import argparse
import hashlib
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request


VERSION = "1.7.0"
ESBUILD_VERSION = "0.28.2"
SOURCE_URL = "https://codeload.github.com/novnc/noVNC/tar.gz/refs/tags/v1.7.0"
SOURCE_SHA256 = "b1003a11b6e6e8d8f7f5e5586daae7f8ca651d8aee0aa155ff9ac841c48f52c6"
PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT = PROJECT_ROOT / "vendor" / "novnc"
LICENSE_FILES = (
    "LICENSE.txt", "AUTHORS", "docs/LICENSE.MPL-2.0",
    "docs/LICENSE.BSD-2-Clause", "docs/LICENSE.BSD-3-Clause",
    "docs/LICENSE.OFL-1.1", "vendor/pako/LICENSE",
)


def legal_comments(source):
    """Mark existing legal headers for preservation in a temporary build tree."""
    def preserve(match):
        comment = match.group(0)
        if not comment.startswith("/*!") and re.search(
            r"copyright|licensed?\s+under|@license|@preserve", comment, re.IGNORECASE
        ):
            return "/*!" + comment[2:]
        return comment
    return re.sub(r"/\*.*?\*/", preserve, source, flags=re.DOTALL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--esbuild", default=shutil.which("esbuild"), help="Path to esbuild 0.28.2 executable")
    parser.add_argument("--source-archive", type=Path, help="Previously downloaded official noVNC 1.7.0 archive")
    args = parser.parse_args()
    if not args.esbuild:
        parser.error("Install the free esbuild 0.28.2 development CLI, or pass its path with --esbuild.")
    esbuild = shutil.which(args.esbuild) or str(Path(args.esbuild).resolve())
    installed = subprocess.run([esbuild, "--version"], check=True, capture_output=True, text=True).stdout.strip()
    if installed != ESBUILD_VERSION:
        parser.error(f"Expected esbuild {ESBUILD_VERSION}; found {installed}.")

    with tempfile.TemporaryDirectory(prefix="nutcracker-novnc-build-") as directory:
        stage = Path(directory)
        archive = args.source_archive
        if archive is None:
            archive = stage / "novnc-source.tar.gz"
            request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "Nutcracker-noVNC-build"})
            with urllib.request.urlopen(request, timeout=60) as response:
                archive.write_bytes(response.read())
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != SOURCE_SHA256:
            raise SystemExit("Upstream archive checksum did not match the pinned source. No bundle was changed.")

        headers = []
        upstream = {}
        with tarfile.open(archive, "r:gz") as package:
            for item in package.getmembers():
                if not item.isfile():
                    continue
                relative = PurePosixPath(item.name).relative_to(f"noVNC-{VERSION}")
                if ".." in relative.parts:
                    raise SystemExit("Unexpected archive path.")
                name = str(relative)
                if not (name in LICENSE_FILES or name.startswith("core/") or name.startswith("vendor/")):
                    continue
                content = package.extractfile(item).read()
                upstream[name] = content
                destination = stage.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if name.endswith(".js"):
                    source = content.decode("utf-8")
                    for header in re.findall(r"/\*.*?\*/", source, flags=re.DOTALL):
                        if re.search(r"copyright|licensed?\s+under|@license|@preserve", header, re.IGNORECASE) and header not in headers:
                            headers.append(header)
                    destination.write_text(legal_comments(source), encoding="utf-8")
                else:
                    destination.write_bytes(content)

        entry = stage / "entry.js"
        entry.write_text(
            'export { default } from "./core/rfb.js";\n'
            'export { initLogging } from "./core/util/logging.js";\n', encoding="utf-8"
        )
        OUTPUT.mkdir(parents=True, exist_ok=True)
        bundle = stage / "rfb.bundle.js"
        banner = (
            f"/*! noVNC {VERSION}, bundled without minification by esbuild {ESBUILD_VERSION}.\n"
            " * Copyright notices and licenses: ./NOTICES.txt\n"
            f" * Exact corresponding upstream source: {SOURCE_URL}\n"
            " * Core licensed under MPL-2.0; bundled dependencies retain their own notices.\n"
            " */"
        )
        subprocess.run([
            esbuild, "entry.js", "--bundle", "--format=esm", "--platform=browser",
            "--target=es2022", "--charset=utf8", "--legal-comments=inline",
            "--tree-shaking=false", f"--banner:js={banner}", f"--outfile={bundle}",
        ], check=True, cwd=stage)

        notice = (
            f"NUTCRACKER — THIRD-PARTY SOFTWARE NOTICES\n\n"
            f"This distribution includes noVNC {VERSION} and its bundled pako dependency.\n"
            f"The noVNC core is licensed under Mozilla Public License 2.0.\n"
            f"Copyright (C) 2022 The noVNC authors; individual source notices follow.\n\n"
            f"Corresponding Source Code Form is freely available at the exact upstream release:\n"
            f"{SOURCE_URL}\n"
            f"Archive SHA-256: {SOURCE_SHA256}\n"
            f"Release page: https://github.com/novnc/noVNC/releases/tag/v{VERSION}\n\n"
            f"The library implementation is unchanged. For bundling, existing legal comments were\n"
            f"marked in a temporary build tree so esbuild preserves them inline. The module\n"
            f"entry exports RFB and initLogging. The output is unminified and built with\n"
            f"esbuild {ESBUILD_VERSION}. build-novnc.py reproduces this procedure from the\n"
            f"verified upstream archive. Nutcracker's viewer integration is separate code.\n\n"
            f"No upstream fonts or artwork are shipped. All license texts referenced by\n"
            f"upstream LICENSE.txt are retained below for completeness.\n"
        )
        for filename in LICENSE_FILES:
            notice += f"\n\n{'=' * 72}\nUPSTREAM FILE: {filename}\n{'=' * 72}\n\n"
            notice += upstream[filename].decode("utf-8")
        notice += f"\n\n{'=' * 72}\nPRESERVED SOURCE COPYRIGHT AND LICENSE HEADERS\n{'=' * 72}\n\n"
        notice += "\n\n".join(headers) + "\n"
        (OUTPUT / "rfb.bundle.js").write_bytes(bundle.read_bytes())
        (OUTPUT / "NOTICES.txt").write_text(notice, encoding="utf-8")
        print("Built vendor/novnc/rfb.bundle.js and vendor/novnc/NOTICES.txt.")


if __name__ == "__main__":
    main()

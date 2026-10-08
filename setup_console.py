#!/usr/bin/env python3
"""Install only the browser-console dependency in this checkout's private runtime."""

from pathlib import Path
import argparse
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-remote", action="store_true", help="Also install the internet gateway dependency.")
    args = parser.parse_args()
    if args.with_remote and sys.version_info < (3, 10):
        print("Internet access needs Python 3.10 or later for the patched networking library.", file=sys.stderr)
        return 1
    if sys.version_info < (3, 9):
        print("The browser console needs Python 3.9 or later.", file=sys.stderr)
        return 1
    root = Path(__file__).resolve().parent
    runtime = root / ".runtime"
    dependencies = runtime / "python-deps"
    if runtime.is_symlink() or dependencies.is_symlink():
        print("The private runtime cannot be a symlink.", file=sys.stderr)
        return 1
    runtime.mkdir(mode=0o700, exist_ok=True)
    dependencies.mkdir(mode=0o700, exist_ok=True)
    print("Installing the tested browser-console library in this folder only.", flush=True)
    # A pure Python wheel works with both macOS's Python 3.9 and newer Python.
    # No system packages, administrator permissions, or native compiler needed.
    result = subprocess.run([
        sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
        "--no-cache-dir", "--no-warn-script-location", "--no-deps",
        "--only-binary=:all:", "--platform", "any", "--upgrade",
        "--target", str(dependencies), "-r", str(root / "requirements.txt"),
    ], shell=False, check=False)
    if result.returncode != 0:
        print("The console library could not be installed. Check your internet connection and retry.", file=sys.stderr)
        return result.returncode
    if args.with_remote:
        result = subprocess.run([
            sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
            "--no-cache-dir", "--no-warn-script-location", "--only-binary=:all:",
            "--target", str(dependencies), "-r", str(root / "requirements-remote.txt"),
        ], shell=False, check=False)
        if result.returncode != 0:
            print("The internet gateway library could not be installed.", file=sys.stderr)
            return result.returncode
    print("Console library ready. Restart Nutcracker to use the browser display.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

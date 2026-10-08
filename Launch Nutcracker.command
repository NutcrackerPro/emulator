#!/bin/zsh
set -e
cd "${0:A:h}"
nutcracker_python="${PWD}/.runtime/python"
if [[ ! -x "$nutcracker_python" ]]; then
  nutcracker_python="$(command -v python3)"
fi
exec "$nutcracker_python" -B start_host.py

#!/bin/zsh
set -e
export PATH="$PATH:$HOME/.docker/bin:/usr/local/bin:/opt/homebrew/bin"
cd -- "${0:A:h:h}"
exec /usr/bin/python3 scripts/validate-santander.py

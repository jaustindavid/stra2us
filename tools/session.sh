#!/bin/bash

# This script must be SOURCED: source session.sh
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    echo "ERROR: Please 'source' this script instead of executing it."
    return 1 2>/dev/null || exit 1
fi

# 1. Update PATH for the current session
HB_PATH="/opt/homebrew/bin"
if [[ -d "$HB_PATH" ]]; then
    export PATH="$HB_PATH:$PATH"
    # Force the shell to forget old binary locations (like /usr/bin/python3)
    hash -r
fi

# 2. Identify the best python3 available now
PYTHON_EXE=$(which python3)

# 4. Create or Activate
if [ ! -d "venv" ]; then
    echo "--- Creating venv with: $PYTHON_EXE ($($PYTHON_EXE --version)) ---"
    "$PYTHON_EXE" -m venv venv
    source venv/bin/activate
    python3 -m pip install --upgrade pip
    pip install -e .
else
    source venv/bin/activate
    echo "Venv Active: $(python3 --version)"
fi

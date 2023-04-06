#!/usr/bin/env bash

set -e # Exit on any error

if [ -z "$1" ]; then
    echo "Usage: $0 <signature hex>"
    exit 1
fi

function are_there_git_changes {
    ! git diff-index --quiet HEAD
}

# Assert there are no git changes
if are_there_git_changes; then
    echo "There are some git changes, please commit them first"
    exit 1
fi

MERKLE_ROOT=$(python cli.py current-merkle-root)

python cli.py sign -s "$1"
git add definitions-latest.json
git commit -m "Sign definitions for $MERKLE_ROOT"

cd definitions-latest
tar cJf ../definitions.tar.xz *
echo "Definitions for deployment stored in definitions.tar.xz"

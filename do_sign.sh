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

python cli.py sign --verify "$1"
git add definitions-latest.json
git commit -m "Sign definitions for $MERKLE_ROOT"

# update the signed branch
git branch --force signed HEAD

python cli.py generate

echo "Don't forget to push main & signed branches:"
echo "  git push origin main"
echo "  git push origin signed"

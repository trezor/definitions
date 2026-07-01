#!/usr/bin/env bash

set -e # Exit on any error

function are_there_git_changes {
    ! git diff-index --quiet HEAD
}

# Assert there are no git changes
if are_there_git_changes; then
    echo "There are some git changes, please commit them first"
    exit 1
fi

# Update all submodules to their latest commit
git submodule update --init --recursive --remote
# Keep the pinned submodule at its fixed commit
git submodule update -- "ethereum/clear-signing-erc7730-registry"

SHOW_ADDED=""
if [[ "$1" == "--show-added" ]]; then SHOW_ADDED="--show-added"; fi

# Download definitions
python cli.py download -v --sleep-duration 2.5 $SHOW_ADDED

# Sign them with dev private keys
python cli.py generate --dev-sign

# Generate coins details
python coins_details/coins_details.py

# Commit with current date in commit message
if are_there_git_changes; then
    git commit -am "Update $(date +'%Y-%m-%d %H:%M:%S')"
fi

#!/usr/bin/env bash

set -e # Exit on any error

function are_there_git_changes {
    ! git diff-index --quiet HEAD
}

ERC7730_ONLY=""
SHOW_ADDED=""
for arg in "$@"; do
    case "$arg" in
        --erc7730-only) ERC7730_ONLY="1" ;;
        --show-added) SHOW_ADDED="--show-added" ;;
    esac
done

# Fast local iteration on the ERC-7730 registry: refresh only the display
# formats and rebuild the signed tarball. Skips the git-clean gate, the
# submodule update, the CoinGecko-heavy coins details, and the auto-commit.
if [[ -n "$ERC7730_ONLY" ]]; then
    python cli.py download -v --erc7730-only $SHOW_ADDED
    python cli.py generate --dev-sign
    exit 0
fi

# Assert there are no git changes
if are_there_git_changes; then
    echo "There are some git changes, please commit them first"
    exit 1
fi

# Update all submodules to their latest commit
git submodule update --init --recursive --remote
# Keep the pinned submodule at its fixed commit
git submodule update -- "ethereum/clear-signing-erc7730-registry"

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

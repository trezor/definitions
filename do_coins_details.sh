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

# Update all submodules
git submodule update --init --recursive --force

# If there are some git changes from submodule update, commit them
if are_there_git_changes; then
    git commit -am "Submodules update"
fi

# Generate coins details
python coins_details/coins_details.py

# Commit with current date in commit message
git commit -am "Coins details update $(date +'%Y-%m-%d %H:%M:%S')"

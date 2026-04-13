#!/bin/bash
# Pre-push hook: warn when pushing to a branch whose PR is already merged.
# Installed via .pre-commit-config.yaml (stages: [pre-push]).

remote="$1"

while read local_ref local_sha remote_ref remote_sha; do
    branch="${remote_ref#refs/heads/}"

    # Skip develop/main
    if [ "$branch" = "develop" ] || [ "$branch" = "main" ]; then
        continue
    fi

    # Check if any merged PR exists for this branch
    merged_pr=$(gh pr list --head "$branch" --state merged --json number --jq '.[0].number' 2>/dev/null)

    if [ -n "$merged_pr" ] && [ "$merged_pr" != "null" ]; then
        echo ""
        echo "⚠️  ERROR: Branch '$branch' was already merged via PR #${merged_pr}."
        echo "   Your push will NOT reach develop. The commit is effectively dead."
        echo "   If you need to fix something, open a new PR instead."
        echo ""
        exit 1
    fi
done

exit 0

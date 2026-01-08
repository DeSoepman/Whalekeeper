#!/bin/bash

# Git push script for Whalekeeper

set -e

echo "📝 Git Push Script"
echo "=================="
echo ""

# Check if there are any changes
if [ -z "$(git status --porcelain)" ]; then
    echo "✓ No changes to commit"
    exit 0
fi

# Show status
echo "Changes to be committed:"
git status --short
echo ""

# Get commit message
if [ -z "$1" ]; then
    echo "Enter commit message:"
    read -r COMMIT_MESSAGE
else
    COMMIT_MESSAGE="$1"
fi

if [ -z "$COMMIT_MESSAGE" ]; then
    echo "❌ Commit message cannot be empty"
    exit 1
fi

# Add all changes
echo ""
echo "Adding all changes..."
git add .

# Commit
echo "Committing changes..."
git commit -m "$COMMIT_MESSAGE"

# Push
echo "Pushing to remote..."
git push

echo ""
echo "✓ Successfully pushed changes!"

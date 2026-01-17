#!/bin/bash
set -e

echo "🔨 Building Whalekeeper with Tests..."
echo ""

# Run tests first
./test.sh

# If tests pass, proceed with build
echo "Building Docker image..."
docker build -t whalekeeper:latest .

echo ""
echo "✅ Build complete!"
echo ""
echo "Run with: docker compose up -d"

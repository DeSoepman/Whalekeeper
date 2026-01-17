#!/bin/bash
set -e

echo "🧪 Running Whalekeeper Tests..."
echo ""

# Check if pytest is installed
if ! python3 -m pytest --version &> /dev/null; then
    echo "❌ pytest not found. Installing test dependencies..."
    pip install -r requirements-dev.txt
    echo ""
fi

# Run tests with coverage
echo "Running tests..."
python3 -m pytest tests/ -v --tb=short

# Check exit code
if [ $? -eq 0 ]; then
    echo ""
    echo "✅ All tests passed!"
    echo ""
    exit 0
else
    echo ""
    echo "❌ Tests failed!"
    echo ""
    exit 1
fi

# Whalekeeper Tests

## Running Tests

### Install Test Dependencies
```bash
pip install -r requirements-dev.txt
```

### Run All Tests
```bash
pytest
```

### Run with Verbose Output
```bash
pytest -v
```

### Run Specific Test File
```bash
pytest tests/test_database.py
```

### Run with Coverage Report
```bash
pytest --cov=app --cov-report=html
```

### Run Only Unit Tests
```bash
pytest -m unit
```

### Run Only Integration Tests
```bash
pytest -m integration
```

## Test Structure

- `tests/test_database.py` - Database operations
- `tests/test_config.py` - Configuration loading
- `tests/test_docker_monitor.py` - Docker monitoring logic
- `tests/test_auth.py` - Authentication
- `tests/test_notifications.py` - Notification service
- `tests/conftest.py` - Shared fixtures

## Build with Tests

Run tests before building:
```bash
./build.sh
```

This will:
1. Run all tests
2. Build Docker image only if tests pass
3. Tag as `whalekeeper:latest`

## Run Tests Only

```bash
./test.sh
```

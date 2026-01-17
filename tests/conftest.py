import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock
from fastapi.testclient import TestClient

from app.database import Database
from app.config import Config


@pytest.fixture
def temp_db():
    """Create a temporary database for testing"""
    with tempfile.NamedTemporaryFile(delete=False, suffix='.db') as f:
        db_path = f.name
    
    db = Database(db_path)
    yield db
    
    # Cleanup
    try:
        os.unlink(db_path)
    except:
        pass


@pytest.fixture
def test_config():
    """Create a test configuration"""
    return Config(
        cron_schedule="0 22 * * 1",
        monitoring={
            "exclude_containers": ["whalekeeper"]
        },
        notifications={
            "email": {"enabled": False},
            "discord": {"enabled": False},
            "webhook": {"enabled": False}
        },
        rollback={"keep_versions": 3},
        web={"host": "0.0.0.0", "port": 5454},
        registry={"username": "", "password": ""}
    )


@pytest.fixture
def mock_docker_client():
    """Mock Docker client for testing"""
    mock = MagicMock()
    
    # Mock containers
    mock_container = MagicMock()
    mock_container.name = "test-container"
    mock_container.id = "abc123"
    mock_container.image = MagicMock()
    mock_container.image.id = "img123"
    mock_container.image.tags = ["test:latest"]
    mock_container.attrs = {
        'Config': {
            'Image': 'test:latest',
            'Env': [],
            'Labels': {},
        },
        'HostConfig': {
            'Binds': [],
            'PortBindings': {},
            'NetworkMode': 'bridge',
            'RestartPolicy': {'Name': 'unless-stopped'},
        }
    }
    
    mock.containers.list.return_value = [mock_container]
    mock.containers.get.return_value = mock_container
    
    # Mock images
    mock_image = MagicMock()
    mock_image.id = "img456"
    mock_image.tags = ["test:v2"]
    mock_image.labels = {}
    
    mock.images.pull.return_value = mock_image
    mock.images.get.return_value = mock_image
    
    return mock


@pytest.fixture
def mock_notifier():
    """Mock notification service"""
    mock = MagicMock()
    mock.send_notification = AsyncMock()
    return mock


@pytest.fixture
def test_client():
    """Create a test client for the FastAPI app"""
    # Import here to avoid circular dependencies
    from app.main import app
    
    return TestClient(app)

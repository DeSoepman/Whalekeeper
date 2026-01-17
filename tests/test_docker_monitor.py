import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from app.docker_monitor import DockerMonitor


@pytest.mark.unit
def test_docker_monitor_init(test_config, temp_db, mock_notifier):
    """Test DockerMonitor initialization"""
    with patch('app.docker_monitor.docker.from_env') as mock_docker:
        mock_docker.return_value = MagicMock()
        
        monitor = DockerMonitor(test_config, temp_db, mock_notifier)
        
        assert monitor.config == test_config
        assert monitor.db == temp_db
        assert monitor.running is False


@pytest.mark.unit
def test_get_monitored_containers(test_config, temp_db, mock_notifier, mock_docker_client):
    """Test getting monitored containers"""
    with patch('app.docker_monitor.docker.from_env') as mock_docker:
        mock_docker.return_value = mock_docker_client
        
        monitor = DockerMonitor(test_config, temp_db, mock_notifier)
        containers = monitor.get_monitored_containers()
        
        # Should exclude 'whalekeeper' from config
        assert len(containers) == 1
        assert containers[0].name == "test-container"


@pytest.mark.unit
def test_get_image_version(test_config, temp_db, mock_notifier):
    """Test extracting version from image"""
    with patch('app.docker_monitor.docker.from_env') as mock_docker:
        mock_docker.return_value = MagicMock()
        
        monitor = DockerMonitor(test_config, temp_db, mock_notifier)
        
        # Mock image with OCI label
        image = MagicMock()
        image.labels = {'org.opencontainers.image.version': '1.2.3'}
        image.tags = ['test:1.2.3']
        image.id = 'sha256:abcdef123456'
        
        version = monitor._get_image_version(image)
        assert version == '1.2.3'


@pytest.mark.unit
def test_get_image_version_from_tag(test_config, temp_db, mock_notifier):
    """Test extracting version from tag when no label"""
    with patch('app.docker_monitor.docker.from_env') as mock_docker:
        mock_docker.return_value = MagicMock()
        
        monitor = DockerMonitor(test_config, temp_db, mock_notifier)
        
        # Mock image without label, but with version tag
        image = MagicMock()
        image.labels = {}
        image.tags = ['myapp:2.0.0']
        image.id = 'sha256:abcdef123456'
        
        version = monitor._get_image_version(image)
        assert version == '2.0.0'


@pytest.mark.unit
def test_get_image_version_fallback(test_config, temp_db, mock_notifier):
    """Test version extraction fallback to generic tags"""
    with patch('app.docker_monitor.docker.from_env') as mock_docker:
        mock_docker.return_value = MagicMock()
        
        monitor = DockerMonitor(test_config, temp_db, mock_notifier)
        
        # Mock image with latest tag (should return 'latest' now, not image ID)
        image = MagicMock()
        image.labels = {}
        image.tags = ['myapp:latest']
        image.id = 'sha256:abcdef123456'
        
        version = monitor._get_image_version(image)
        assert version == 'latest'  # Returns tag even if it's generic


@pytest.mark.unit
def test_get_container_config(test_config, temp_db, mock_notifier, mock_docker_client):
    """Test extracting container configuration"""
    with patch('app.docker_monitor.docker.from_env') as mock_docker:
        mock_docker.return_value = mock_docker_client
        
        monitor = DockerMonitor(test_config, temp_db, mock_notifier)
        
        container = mock_docker_client.containers.list()[0]
        config = monitor.get_container_config(container)
        
        assert config['name'] == 'test-container'
        assert config['image'] == 'test:latest'
        assert config['network_mode'] == 'bridge'
        assert config['restart_policy']['Name'] == 'unless-stopped'


@pytest.mark.unit
def test_has_update_cache(test_config, temp_db, mock_notifier):
    """Test update cache functionality"""
    with patch('app.docker_monitor.docker.from_env') as mock_docker:
        mock_docker.return_value = MagicMock()
        
        monitor = DockerMonitor(test_config, temp_db, mock_notifier)
        
        # Initially no updates
        assert monitor.has_update("test-container") is False
        
        # Add to cache
        monitor.update_cache["test-container"] = {"container": "data"}
        assert monitor.has_update("test-container") is True


@pytest.mark.unit
@patch('app.docker_monitor.docker.from_env')
def test_check_for_updates_no_update(mock_docker_from_env, test_config, temp_db, mock_notifier):
    """Test checking for updates when none available"""
    mock_client = MagicMock()
    mock_docker_from_env.return_value = mock_client
    
    # Mock container and image
    mock_container = MagicMock()
    mock_container.name = "test-container"
    mock_container.image = MagicMock()
    mock_container.image.id = "img123"
    mock_container.image.tags = ["test:v1"]
    mock_container.attrs = {
        'Config': {'Image': 'test:v1'}
    }
    
    # Mock pull returns same image
    mock_client.images.pull.return_value = mock_container.image
    
    monitor = DockerMonitor(test_config, temp_db, mock_notifier)
    result = monitor.check_for_updates(mock_container)
    
    assert result is None  # No update available


@pytest.mark.unit
@patch('app.docker_monitor.docker.from_env')
def test_check_for_updates_with_update(mock_docker_from_env, test_config, temp_db, mock_notifier):
    """Test checking for updates when update is available"""
    mock_client = MagicMock()
    mock_docker_from_env.return_value = mock_client
    
    # Mock container
    mock_container = MagicMock()
    mock_container.name = "test-container"
    mock_container.image = MagicMock()
    mock_container.image.id = "img_old_123"
    mock_container.image.tags = ["test:v1"]
    mock_container.attrs = {
        'Config': {'Image': 'test:v1'}
    }
    
    # Mock pull returns different image
    new_image = MagicMock()
    new_image.id = "img_new_456"
    new_image.tags = ["test:v1"]
    mock_client.images.pull.return_value = new_image
    
    monitor = DockerMonitor(test_config, temp_db, mock_notifier)
    result = monitor.check_for_updates(mock_container)
    
    assert result is not None
    assert result['old_image'].id == "img_old_123"
    assert result['new_image'].id == "img_new_456"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rollback_container_success(test_config, temp_db, mock_notifier):
    """Test successful container rollback"""
    with patch('app.docker_monitor.docker.from_env') as mock_docker:
        mock_client = MagicMock()
        mock_docker.return_value = mock_client
        
        monitor = DockerMonitor(test_config, temp_db, mock_notifier)
        
        # Save a version first
        temp_db.save_image_version(
            container_name="test-container",
            image_name="test:1.0.0",
            image_id="old_img_123",
            image_tag="1.0.0",
            container_config={
                'name': 'test-container',
                'image': 'test:1.0.0',
                'environment': {},
                'volumes': [],
                'ports': {},
                'network_mode': 'bridge',
                'restart_policy': {'Name': 'unless-stopped'},
            }
        )
        
        # Get the version ID
        versions = temp_db.get_image_versions("test-container")
        version_id = versions[0]['id']
        
        # Mock current container
        current_container = MagicMock()
        current_container.name = "test-container"
        current_container.image.id = "current_img_456"
        current_container.image.tags = ["test:2.0.0"]
        current_container.image.labels = {'org.opencontainers.image.version': '2.0.0'}
        mock_client.containers.get.return_value = current_container
        
        # Mock old image
        old_image = MagicMock()
        old_image.id = "old_img_123"
        old_image.tags = ["test:1.0.0"]
        old_image.labels = {'org.opencontainers.image.version': '1.0.0'}
        mock_client.images.get.return_value = old_image
        
        # Mock container creation
        new_container = MagicMock()
        new_container.id = "new_container_789"
        mock_client.containers.run.return_value = new_container
        
        # Perform rollback
        result = await monitor.rollback_container("test-container", version_id)
        
        # Verify success
        assert result['success'] is True
        assert 'best_tag' in result
        
        # Verify old container was stopped and removed
        current_container.stop.assert_called_once()
        current_container.remove.assert_called_once()
        
        # Verify new container was created
        mock_client.containers.run.assert_called_once()
        
        # Verify history was logged
        history = temp_db.get_update_history(limit=1)
        assert len(history) > 0
        assert history[0]['status'] == 'rollback'
        assert history[0]['container_name'] == 'test-container'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rollback_container_failure(test_config, temp_db, mock_notifier):
    """Test rollback failure handling and error logging"""
    with patch('app.docker_monitor.docker.from_env') as mock_docker:
        mock_client = MagicMock()
        mock_docker.return_value = mock_client
        
        monitor = DockerMonitor(test_config, temp_db, mock_notifier)
        
        # Save a version
        temp_db.save_image_version(
            container_name="test-container",
            image_name="test:1.0.0",
            image_id="old_img_123",
            image_tag="1.0.0",
            container_config={'name': 'test-container'}
        )
        
        versions = temp_db.get_image_versions("test-container")
        version_id = versions[0]['id']
        
        # Mock image retrieval to fail
        mock_client.images.get.side_effect = Exception("Image not found")
        
        # Perform rollback
        result = await monitor.rollback_container("test-container", version_id)
        
        # Verify failure
        assert result['success'] is False
        assert 'error' in result
        
        # Verify error was logged to database
        history = temp_db.get_update_history(limit=1)
        assert len(history) > 0
        assert history[0]['status'] == 'failed'
        assert history[0]['container_name'] == 'test-container'
        assert 'Rollback failed' in history[0]['message']

import pytest
from unittest.mock import Mock, AsyncMock, patch
from app.docker_monitor import DockerMonitor
from app.config import Config, MonitoringConfig
from app.database import Database
from app.notifications import NotificationService


@pytest.fixture
def mock_config():
    """Create a mock config with dependency restart enabled"""
    config = Config()
    config.monitoring = MonitoringConfig(
        exclude_containers=[],
        auto_restart_dependents=True
    )
    return config


@pytest.fixture
def mock_db():
    """Create a mock database"""
    db = Mock(spec=Database)
    db.add_update_history = Mock()
    return db


@pytest.fixture
def mock_notifier():
    """Create a mock notifier"""
    return Mock(spec=NotificationService)


@pytest.fixture
def docker_monitor(mock_config, mock_db, mock_notifier):
    """Create a DockerMonitor instance with mocks"""
    with patch('app.docker_monitor.docker.from_env'):
        monitor = DockerMonitor(mock_config, mock_db, mock_notifier)
        return monitor


def test_config_auto_restart_dependents_enabled(mock_config):
    """Test that auto_restart_dependents config option is available and enabled by default"""
    assert hasattr(mock_config.monitoring, 'auto_restart_dependents')
    assert mock_config.monitoring.auto_restart_dependents is True


def test_config_auto_restart_dependents_disabled():
    """Test that auto_restart_dependents can be disabled"""
    config = Config()
    config.monitoring = MonitoringConfig(
        exclude_containers=[],
        auto_restart_dependents=False
    )
    assert config.monitoring.auto_restart_dependents is False


def test_detect_dependent_containers_network_mode(docker_monitor):
    """Test detection of containers using network_mode: container:<name>"""
    # Mock containers
    parent_container = Mock()
    parent_container.name = 'gluetun'
    
    dependent_container = Mock()
    dependent_container.name = 'qbittorrent'
    dependent_container.attrs = {
        'HostConfig': {
            'NetworkMode': 'container:gluetun',
            'Links': [],
            'VolumesFrom': None
        }
    }
    
    independent_container = Mock()
    independent_container.name = 'nginx'
    independent_container.attrs = {
        'HostConfig': {
            'NetworkMode': 'bridge',
            'Links': [],
            'VolumesFrom': None
        }
    }
    
    # Mock client.containers.list()
    docker_monitor.client.containers.list = Mock(return_value=[
        parent_container,
        dependent_container,
        independent_container
    ])
    
    # Test dependency detection
    dependents = docker_monitor.detect_dependent_containers('gluetun')
    
    assert len(dependents) == 1
    assert dependents[0].name == 'qbittorrent'


def test_detect_dependent_containers_links(docker_monitor):
    """Test detection of containers using --link"""
    parent_container = Mock()
    parent_container.name = 'database'
    
    dependent_container = Mock()
    dependent_container.name = 'webapp'
    dependent_container.attrs = {
        'HostConfig': {
            'NetworkMode': 'bridge',
            'Links': ['/database:/webapp/db'],
            'VolumesFrom': None
        }
    }
    
    docker_monitor.client.containers.list = Mock(return_value=[
        parent_container,
        dependent_container
    ])
    
    dependents = docker_monitor.detect_dependent_containers('database')
    
    assert len(dependents) == 1
    assert dependents[0].name == 'webapp'


def test_detect_dependent_containers_volumes_from(docker_monitor):
    """Test detection of containers using volumes_from"""
    parent_container = Mock()
    parent_container.name = 'data-container'
    
    dependent_container = Mock()
    dependent_container.name = 'app'
    dependent_container.attrs = {
        'HostConfig': {
            'NetworkMode': 'bridge',
            'Links': [],
            'VolumesFrom': ['data-container']
        }
    }
    
    docker_monitor.client.containers.list = Mock(return_value=[
        parent_container,
        dependent_container
    ])
    
    dependents = docker_monitor.detect_dependent_containers('data-container')
    
    assert len(dependents) == 1
    assert dependents[0].name == 'app'


@pytest.mark.asyncio
async def test_restart_dependent_containers_success(docker_monitor, mock_db):
    """Test successful restart of dependent containers"""
    dependent_container = Mock()
    dependent_container.name = 'qbittorrent'
    dependent_container.id = 'abc123'
    dependent_container.status = 'running'
    dependent_container.restart = Mock()
    dependent_container.reload = Mock()
    
    docker_monitor.detect_dependent_containers = Mock(return_value=[dependent_container])
    
    # Test restart
    results = await docker_monitor.restart_dependent_containers('gluetun')
    
    # Verify restart was called
    dependent_container.restart.assert_called_once_with(timeout=30)
    
    # Verify success
    assert 'qbittorrent' in results
    assert results['qbittorrent'] is True
    
    # Verify database entry
    mock_db.add_update_history.assert_called_once()
    call_args = mock_db.add_update_history.call_args[1]
    assert call_args['container_name'] == 'qbittorrent'
    assert call_args['status'] == 'restarted'


@pytest.mark.asyncio
async def test_restart_dependent_containers_disabled(docker_monitor, mock_db):
    """Test that restart is skipped when auto_restart_dependents is disabled"""
    docker_monitor.config.monitoring.auto_restart_dependents = False
    
    results = await docker_monitor.restart_dependent_containers('gluetun')
    
    # Should return empty dict without doing anything
    assert results == {}
    mock_db.add_update_history.assert_not_called()


@pytest.mark.asyncio
async def test_restart_dependent_containers_failure(docker_monitor, mock_db):
    """Test handling of restart failure"""
    dependent_container = Mock()
    dependent_container.name = 'qbittorrent'
    dependent_container.id = 'abc123'
    dependent_container.restart = Mock(side_effect=Exception("Restart failed"))
    
    docker_monitor.detect_dependent_containers = Mock(return_value=[dependent_container])
    
    # Test restart
    results = await docker_monitor.restart_dependent_containers('gluetun')
    
    # Verify failure
    assert 'qbittorrent' in results
    assert results['qbittorrent'] is False
    
    # Verify failure was recorded
    assert mock_db.add_update_history.call_count == 1
    call_args = mock_db.add_update_history.call_args[1]
    assert call_args['container_name'] == 'qbittorrent'
    assert call_args['status'] == 'failed'


@pytest.mark.asyncio
async def test_restart_dependent_containers_no_dependents(docker_monitor):
    """Test restart when no dependent containers exist"""
    docker_monitor.detect_dependent_containers = Mock(return_value=[])
    
    results = await docker_monitor.restart_dependent_containers('standalone-container')
    
    # Should return empty dict
    assert results == {}

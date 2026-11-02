import docker
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
    dependent_container.attrs = {'HostConfig': {'NetworkMode': 'bridge'}}
    
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
    dependent_container.attrs = {'HostConfig': {'NetworkMode': 'bridge'}}
    
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


# ---------------------------------------------------------------------------
# Regression tests: containers sharing a parent's network namespace
#
# Docker resolves `network_mode: "service:gluetun"` to `container:<64-char id>`
# at create time. Matching that against `container:<name>` never succeeds, so
# dependents went undetected and were left pointing at a removed container.
# ---------------------------------------------------------------------------

GLUETUN_ID = '99b6fec3fb5f8000d824b6380b758b88ce74ba5152524b3c83dcd6159f5173d3'
OTHER_ID = 'aa11bb22cc33dd44ee55ff66aa77bb88cc99dd00ee11ff22aa33bb44cc55dd66'


def _make_parent(name='gluetun', container_id=GLUETUN_ID):
    parent = Mock()
    parent.name = name
    parent.id = container_id
    parent.attrs = {'HostConfig': {'NetworkMode': 'bridge', 'Links': [], 'VolumesFrom': None}}
    return parent


def _make_netns_dependent(network_mode, name='qbittorrent'):
    dependent = Mock()
    dependent.name = name
    dependent.id = 'dep0000000001'
    dependent.attrs = {
        'HostConfig': {'NetworkMode': network_mode, 'Links': [], 'VolumesFrom': None}
    }
    return dependent


def test_detect_dependent_by_resolved_container_id(docker_monitor):
    """Dependents must be found when Docker stored the parent's full container ID"""
    parent = _make_parent()
    dependent = _make_netns_dependent(f'container:{GLUETUN_ID}')

    docker_monitor.client.containers.list = Mock(return_value=[parent, dependent])

    dependents = docker_monitor.detect_dependent_containers('gluetun')

    assert [c.name for c in dependents] == ['qbittorrent']


def test_detect_dependent_by_short_container_id(docker_monitor):
    """Docker accepts short IDs, so detection must handle them too"""
    parent = _make_parent()
    dependent = _make_netns_dependent(f'container:{GLUETUN_ID[:12]}')

    docker_monitor.client.containers.list = Mock(return_value=[parent, dependent])

    dependents = docker_monitor.detect_dependent_containers('gluetun')

    assert [c.name for c in dependents] == ['qbittorrent']


def test_detect_dependent_ignores_unrelated_container_id(docker_monitor):
    """A container sharing a *different* container's namespace is not a dependent"""
    parent = _make_parent()
    unrelated = _make_netns_dependent(f'container:{OTHER_ID}', name='other-app')

    docker_monitor.client.containers.list = Mock(return_value=[parent, unrelated])

    dependents = docker_monitor.detect_dependent_containers('gluetun')

    assert dependents == []


def test_detect_dependent_volumes_from_with_access_mode(docker_monitor):
    """volumes_from entries may carry a :ro/:rw suffix"""
    parent = _make_parent(name='data-container', container_id=OTHER_ID)
    dependent = Mock()
    dependent.name = 'app'
    dependent.id = 'dep2'
    dependent.attrs = {
        'HostConfig': {'NetworkMode': 'bridge', 'Links': [], 'VolumesFrom': ['data-container:ro']}
    }

    docker_monitor.client.containers.list = Mock(return_value=[parent, dependent])

    dependents = docker_monitor.detect_dependent_containers('data-container')

    assert [c.name for c in dependents] == ['app']


def test_netns_parent_ref():
    """Only container: network modes reference a namespace parent"""
    assert DockerMonitor.netns_parent_ref(f'container:{GLUETUN_ID}') == GLUETUN_ID
    assert DockerMonitor.netns_parent_ref('container:gluetun') == 'gluetun'
    assert DockerMonitor.netns_parent_ref('bridge') is None
    assert DockerMonitor.netns_parent_ref('host') is None
    assert DockerMonitor.netns_parent_ref('') is None
    assert DockerMonitor.netns_parent_ref(None) is None


def test_build_run_kwargs_drops_options_owned_by_namespace_parent(docker_monitor):
    """
    Docker rejects hostname/ports/extra_hosts alongside container: network mode with
    'conflicting options: hostname and the network mode' (HTTP 400).
    """
    container_config = {
        'name': 'qbittorrent',
        'environment': ['PUID=111'],
        'volumes': ['/opt/qbittorrent/config:/config'],
        'ports': {'8080/tcp': [{'HostPort': '8080'}]},
        'network_mode': f'container:{GLUETUN_ID}',
        # Docker auto-assigns the namespace parent's ID as the hostname
        'hostname': GLUETUN_ID[:12],
        'extra_hosts': ['example.com:1.2.3.4'],
        'restart_policy': {'Name': 'always'},
    }

    kwargs = docker_monitor.build_run_kwargs(container_config, 'sha256:newimage')

    assert 'hostname' not in kwargs
    assert 'ports' not in kwargs
    assert 'extra_hosts' not in kwargs
    # Everything unrelated to the namespace survives
    assert kwargs['network_mode'] == f'container:{GLUETUN_ID}'
    assert kwargs['name'] == 'qbittorrent'
    assert kwargs['environment'] == ['PUID=111']
    assert kwargs['volumes'] == ['/opt/qbittorrent/config:/config']
    assert kwargs['restart_policy'] == {'Name': 'always'}


def test_build_run_kwargs_keeps_options_for_normal_containers(docker_monitor):
    """Containers with their own namespace keep hostname and port bindings"""
    container_config = {
        'name': 'sonarr',
        'network_mode': 'bridge',
        'hostname': 'sonarr-host',
        'ports': {'8989/tcp': [{'HostPort': '8989'}]},
        'volumes': [],
    }

    kwargs = docker_monitor.build_run_kwargs(container_config, 'sha256:newimage')

    assert kwargs['hostname'] == 'sonarr-host'
    assert kwargs['ports'] == {'8989/tcp': [{'HostPort': '8989'}]}


def test_recreate_netns_dependent_points_at_new_parent(docker_monitor):
    """Recreation must rewrite the stale parent ID, which restart() cannot do"""
    new_parent_id = 'f' * 64

    dependent = Mock()
    dependent.name = 'qbittorrent'
    dependent.id = 'dep0000000001'
    dependent.image.id = 'sha256:qbitimage'
    dependent.attrs = {
        'Config': {
            'Image': 'emmercm/qbittorrent:latest',
            'Env': ['PUID=111'],
            'Labels': {},
            'Hostname': GLUETUN_ID[:12],
            'Cmd': None,
            'Entrypoint': None,
            'WorkingDir': '',
            'User': '',
        },
        'HostConfig': {
            'NetworkMode': f'container:{GLUETUN_ID}',
            'Binds': ['/opt/qbittorrent/config:/config'],
            'PortBindings': {},
            'RestartPolicy': {'Name': 'always'},
        },
        'NetworkSettings': {'Networks': {}},
    }
    dependent.stop = Mock()
    dependent.remove = Mock()

    recreated = Mock()
    docker_monitor.client.containers.run = Mock(return_value=recreated)

    result = docker_monitor.recreate_netns_dependent(dependent, new_parent_id)

    dependent.stop.assert_called_once()
    dependent.remove.assert_called_once()
    assert result is recreated

    run_kwargs = docker_monitor.client.containers.run.call_args[1]
    assert run_kwargs['network_mode'] == f'container:{new_parent_id}'
    assert run_kwargs['name'] == 'qbittorrent'
    assert run_kwargs['image'] == 'sha256:qbitimage'
    # The stale hostname must not be replayed, or Docker returns a 400
    assert 'hostname' not in run_kwargs


@pytest.mark.asyncio
async def test_restart_dependent_containers_recreates_netns_dependent(docker_monitor, mock_db):
    """With a new parent ID, netns dependents are recreated rather than restarted"""
    new_parent_id = 'f' * 64

    dependent = _make_netns_dependent(f'container:{GLUETUN_ID}')
    dependent.start = Mock()
    dependent.restart = Mock()

    recreated = Mock()
    recreated.name = 'qbittorrent'
    recreated.id = 'newdep000001'
    recreated.status = 'running'
    recreated.reload = Mock()

    docker_monitor.detect_dependent_containers = Mock(return_value=[dependent])
    docker_monitor.recreate_netns_dependent = Mock(return_value=recreated)

    results = await docker_monitor.restart_dependent_containers(
        'gluetun', new_parent_id=new_parent_id
    )

    docker_monitor.recreate_netns_dependent.assert_called_once_with(dependent, new_parent_id)
    # A plain restart would fail with exit 128 against the removed parent
    dependent.start.assert_not_called()
    dependent.restart.assert_not_called()
    assert results == {'qbittorrent': True}


@pytest.mark.asyncio
async def test_restart_dependent_containers_records_recreate_failure(docker_monitor, mock_db):
    """A failed recreate is reported, since the old container is already gone"""
    dependent = _make_netns_dependent(f'container:{GLUETUN_ID}')

    docker_monitor.detect_dependent_containers = Mock(return_value=[dependent])
    docker_monitor.recreate_netns_dependent = Mock(side_effect=Exception("create failed"))

    results = await docker_monitor.restart_dependent_containers(
        'gluetun', new_parent_id='f' * 64
    )

    assert results == {'qbittorrent': False}
    call_args = mock_db.add_update_history.call_args[1]
    assert call_args['status'] == 'failed'
    assert 'docker compose up -d qbittorrent' in call_args['message']


def test_reconnect_networks_skipped_for_netns_container(docker_monitor):
    """Attaching a namespace-sharing container to a network is invalid"""
    docker_monitor.client.networks = Mock()

    result = docker_monitor.reconnect_networks(
        Mock(),
        {'name': 'qbittorrent', 'network_mode': f'container:{GLUETUN_ID}',
         'networks': {'opt_default': {'aliases': ['qbittorrent']}}},
    )

    assert result is True
    docker_monitor.client.networks.get.assert_not_called()


@pytest.mark.asyncio
async def test_restart_dependent_containers_skips_recreate_when_already_current(
    docker_monitor, mock_db
):
    """
    The compose path may already have recreated the dependent against the new parent.
    Recreating is not atomic, so a dependent that is already correct is left alone.
    """
    new_parent_id = 'f' * 64

    dependent = _make_netns_dependent(f'container:{new_parent_id}')
    dependent.status = 'running'
    dependent.reload = Mock()
    dependent.start = Mock()
    dependent.restart = Mock()

    docker_monitor.detect_dependent_containers = Mock(return_value=[dependent])
    docker_monitor.recreate_netns_dependent = Mock()

    results = await docker_monitor.restart_dependent_containers(
        'gluetun', new_parent_id=new_parent_id
    )

    docker_monitor.recreate_netns_dependent.assert_not_called()
    dependent.restart.assert_called_once()
    assert results == {'qbittorrent': True}


def test_ref_matches_id_short_and_full():
    """Short IDs are a prefix of the full ID; stray fragments must not match"""
    assert DockerMonitor.ref_matches_id(GLUETUN_ID, GLUETUN_ID)
    assert DockerMonitor.ref_matches_id(GLUETUN_ID[:12], GLUETUN_ID)
    assert not DockerMonitor.ref_matches_id(OTHER_ID, GLUETUN_ID)
    assert not DockerMonitor.ref_matches_id(GLUETUN_ID[:3], GLUETUN_ID)
    assert not DockerMonitor.ref_matches_id('', GLUETUN_ID)
    assert not DockerMonitor.ref_matches_id(GLUETUN_ID, '')


@pytest.mark.asyncio
async def test_rollback_drops_options_owned_by_namespace_parent(docker_monitor):
    """
    Rolling back a container that joins another container's namespace must not
    replay hostname/ports, or Docker answers 400 and the rollback fails outright.
    """
    container_config = {
        'name': 'qbittorrent',
        'network_mode': f'container:{GLUETUN_ID}',
        'hostname': GLUETUN_ID[:12],
        'ports': {'8080/tcp': [{'HostPort': '8080'}]},
        'volumes': ['/opt/qbittorrent/config:/config'],
    }

    docker_monitor.client.containers.get = Mock(side_effect=docker.errors.NotFound('gone'))
    docker_monitor.client.images.get = Mock(return_value=Mock(id='sha256:oldimage'))
    docker_monitor.client.containers.run = Mock(return_value=Mock())
    docker_monitor.reconnect_networks = Mock(return_value=True)

    result = await docker_monitor.rollback_after_failed_update(
        'qbittorrent', 'sha256:oldimage', container_config, 'health check failed'
    )

    assert result is True
    run_kwargs = docker_monitor.client.containers.run.call_args[1]
    assert 'hostname' not in run_kwargs
    assert 'ports' not in run_kwargs
    assert run_kwargs['image'] == 'sha256:oldimage'


# Regression tests: gluetun failing its health check left qbittorrent running in the
# network namespace of a removed container, until 'docker compose up -d' was run.
def _health_monitor(statuses):
    """A monitor whose container reports the given health statuses, one per check."""
    monitor = _rollback_monitor()
    container = Mock()
    container.status = 'running'
    remaining = iter(statuses)

    def reload():
        container.attrs = {
            'Config': {'Healthcheck': {'Test': ['CMD', 'true']}},
            'State': {'Health': {'Status': next(remaining)}},
        }

    container.reload = reload
    monitor.client.containers.get.return_value = container
    return monitor


def _rollback_monitor():
    with patch('docker.from_env'):
        monitor = DockerMonitor(Config(), Mock(), AsyncMock())
    return monitor


@pytest.mark.asyncio
async def test_slow_start_is_not_treated_as_failure():
    monitor = _health_monitor(['unhealthy', 'unhealthy', 'unhealthy', 'unhealthy', 'unhealthy', 'healthy'])
    with patch('app.docker_monitor.asyncio.sleep', new=AsyncMock()):
        healthy, reason = await monitor.monitor_container_health('gluetun', 'old')
    assert healthy is True
    assert reason == ""


@pytest.mark.asyncio
async def test_container_that_stays_unhealthy_fails():
    monitor = _health_monitor(['unhealthy'] * 200)
    with patch('app.docker_monitor.asyncio.sleep', new=AsyncMock()):
        healthy, reason = await monitor.monitor_container_health('gluetun', 'old')
    assert healthy is False
    assert 'unhealthy' in reason


@pytest.mark.asyncio
async def test_rollback_reattaches_namespace_dependent():
    monitor = _rollback_monitor()
    dependent = _make_netns_dependent(f'container:{GLUETUN_ID}')
    monitor.detect_dependent_containers = Mock(return_value=[dependent])
    monitor.restart_dependent_containers = AsyncMock(return_value={'qbittorrent': True})
    monitor.reconnect_networks = Mock(return_value=True)
    monitor.client.containers.run.return_value = Mock(id='rolledback')

    result = await monitor.rollback_after_failed_update(
        'gluetun', 'sha256:old', {'name': 'gluetun', 'network_mode': 'bridge'}, 'unhealthy'
    )

    assert result is True
    monitor.restart_dependent_containers.assert_awaited_once_with(
        'gluetun', pre_stopped=[dependent], new_parent_id='rolledback'
    )


@pytest.mark.asyncio
async def test_rollback_detects_dependents_before_removing_failed_container():
    monitor = _rollback_monitor()
    calls = []
    monitor.detect_dependent_containers = Mock(side_effect=lambda name: calls.append('detect') or [])
    failed = Mock()
    failed.remove.side_effect = lambda: calls.append('remove')
    monitor.client.containers.get.return_value = failed
    monitor.reconnect_networks = Mock(return_value=True)
    monitor.client.containers.run.return_value = Mock(id='rolledback')

    await monitor.rollback_after_failed_update('gluetun', 'sha256:old', {'name': 'gluetun'}, 'x')

    assert calls == ['detect', 'remove']


def test_rollback_skips_stale_pre_stopped_dependent():
    monitor = _rollback_monitor()
    monitor.detect_dependent_containers = Mock(return_value=[])
    stale = _make_netns_dependent(f'container:{GLUETUN_ID}')
    stale.reload = Mock(side_effect=docker.errors.NotFound('gone'))
    stopped = _make_netns_dependent(f'container:{GLUETUN_ID}', name='other')
    stopped.reload = Mock()

    assert monitor._dependents_for_rollback('gluetun', [stale, stopped]) == [stopped]


@pytest.mark.asyncio
async def test_failed_start_rolls_back_and_brings_dependents_back():
    monitor = _rollback_monitor()
    monitor._get_image_version = Mock(return_value='v1')
    monitor.get_container_config = Mock(return_value={'name': 'gluetun'})
    dependent = _make_netns_dependent(f'container:{GLUETUN_ID}')
    monitor.stop_dependent_containers = Mock(return_value=[dependent])
    monitor.client.containers.run.side_effect = docker.errors.APIError('start failed')
    monitor.rollback_after_failed_update = AsyncMock(return_value=True)
    container = Mock(labels={}, id='old-id')
    container.name = 'gluetun'
    info = {'container': container, 'old_image': Mock(id='sha256:old'),
            'new_image': Mock(id='sha256:new'), 'image_name': 'gluetun:latest'}

    assert await monitor.update_container(info, send_notification=False) is False

    args = monitor.rollback_after_failed_update.await_args.args
    assert args[0:2] == ('gluetun', 'sha256:old')
    assert args[4] == [dependent]


@pytest.mark.asyncio
async def test_failure_before_removal_does_not_roll_back():
    monitor = _rollback_monitor()
    monitor._get_image_version = Mock(return_value='v1')
    monitor.get_container_config = Mock(return_value={'name': 'gluetun'})
    monitor.stop_dependent_containers = Mock(return_value=[])
    monitor.rollback_after_failed_update = AsyncMock()
    container = Mock(labels={}, id='old-id')
    container.name = 'gluetun'
    container.stop.side_effect = docker.errors.APIError('cannot stop')
    info = {'container': container, 'old_image': Mock(id='sha256:old'),
            'new_image': Mock(id='sha256:new'), 'image_name': 'gluetun:latest'}

    assert await monitor.update_container(info, send_notification=False) is False
    monitor.rollback_after_failed_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_skips_stale_snapshot_of_recreated_dependent():
    monitor = _rollback_monitor()
    snapshot = Mock(id='old-qbit')
    snapshot.name = 'qbittorrent'
    fresh = Mock(id='new-qbit')
    fresh.name = 'qbittorrent'
    monitor.get_monitored_containers = Mock(return_value=[snapshot])

    monitor.client.containers.get.return_value = fresh
    monitor.check_for_updates = Mock(return_value=None)
    monitor.send_summary_notification = AsyncMock()

    await monitor.check_all_containers()

    monitor.check_for_updates.assert_called_once_with(fresh)


@pytest.mark.asyncio
async def test_batch_skips_container_removed_during_run():
    monitor = _rollback_monitor()
    snapshot = Mock(id='old')
    snapshot.name = 'gone'
    monitor.get_monitored_containers = Mock(return_value=[snapshot])
    monitor.client.containers.get.side_effect = docker.errors.NotFound('gone')
    monitor.check_for_updates = Mock()
    monitor.send_summary_notification = AsyncMock()

    await monitor.check_all_containers()

    monitor.check_for_updates.assert_not_called()


def _config_of(container_config, image_config):
    with patch('docker.from_env'):
        monitor = DockerMonitor(Config(), Mock(), AsyncMock())
    container = Mock()
    container.name = 'gluetun'
    container.attrs = {'Config': container_config, 'HostConfig': {}, 'NetworkSettings': {}}
    container.image.attrs = {'Config': image_config}
    return monitor.get_container_config(container)


def test_image_defaults_are_not_frozen_into_new_container():
    config = _config_of(
        {'Image': 'gluetun', 'Env': ['WIREGUARD_MTU=1400', 'DOT=on', 'VPN_TYPE=wireguard', 'PATH=/bin'],
         'Entrypoint': ['/gluetun-entrypoint'], 'Cmd': None, 'WorkingDir': '/', 'User': ''},
        {'Env': ['WIREGUARD_MTU=1400', 'DOT=on', 'VPN_TYPE=openvpn', 'PATH=/bin'],
         'Entrypoint': ['/gluetun-entrypoint'], 'Cmd': None, 'WorkingDir': '/', 'User': ''},
    )
    assert config['environment'] == ['VPN_TYPE=wireguard']
    assert config['entrypoint'] is None
    assert config['working_dir'] is None
    assert config['user'] is None


def test_values_set_for_the_container_are_kept():
    config = _config_of(
        {'Image': 'gluetun', 'Env': ['A=1'], 'Entrypoint': ['/custom'], 'Cmd': ['run'], 'WorkingDir': '/app', 'User': '1000'},
        {'Env': [], 'Entrypoint': ['/default'], 'Cmd': ['serve'], 'WorkingDir': '/', 'User': ''},
    )
    assert config['environment'] == ['A=1']
    assert config['entrypoint'] == ['/custom']
    assert config['command'] == ['run']
    assert config['working_dir'] == '/app'
    assert config['user'] == '1000'


def test_all_values_kept_when_image_is_gone():
    with patch('docker.from_env'):
        monitor = DockerMonitor(Config(), Mock(), AsyncMock())
    container = Mock()
    container.name = 'gluetun'
    container.attrs = {'Config': {'Image': 'gluetun', 'Env': ['A=1'], 'Cmd': ['run']}, 'HostConfig': {}, 'NetworkSettings': {}}
    type(container).image = property(lambda self: (_ for _ in ()).throw(docker.errors.ImageNotFound('gone')))

    config = monitor.get_container_config(container)

    assert config['environment'] == ['A=1']
    assert config['command'] == ['run']

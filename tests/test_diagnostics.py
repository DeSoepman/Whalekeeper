"""Tests for failure reporting to the log server."""
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import docker
import pytest

from app.diagnostics import (
    ALREADY_REPORTED,
    DiagnosticsReporter,
    ErrorLogForwarder,
    LogBuffer,
    container_log_tail,
    create_reporter,
    describe_container,
    fit_payload,
    get_instance_id,
    redact,
    redact_value,
)
from app.docker_monitor import DockerMonitor


def make_reporter(buffer=None):
    return DiagnosticsReporter(
        url="https://logs.example.com/",
        token="token",
        instance_id="a" * 32,
        instance_name="host",
        version="1.0.0",
        log_buffer=buffer or LogBuffer(),
    )


def make_container(name="nginx", status="created", error=""):
    container = MagicMock()
    container.name = name
    container.id = f"{name}-id"
    container.labels = {}
    container.attrs = {
        "State": {"Status": status, "Error": error, "ExitCode": 128, "OOMKilled": False},
        "Config": {"Image": "nginx:latest", "Env": ["DB_PASSWORD=hunter2"], "Labels": {}},
        "HostConfig": {"NetworkMode": "bridge", "RestartPolicy": {"Name": "always"}},
        "RestartCount": 0,
    }
    return container


@pytest.fixture
def monitor(test_config):
    with patch("app.docker_monitor.docker.from_env"):
        instance = DockerMonitor(test_config, MagicMock(), AsyncMock())
    instance.diagnostics = MagicMock()
    instance._get_image_version = MagicMock(return_value="v1")
    return instance


def update_info(container):
    return {
        "container": container,
        "old_image": MagicMock(id="sha256:old"),
        "new_image": MagicMock(id="sha256:new"),
        "image_name": "nginx:latest",
    }


class TestRedact:
    @pytest.mark.parametrize("text,secret", [
        ("password=hunter2 next", "hunter2"),
        ('"smtp_password": "hunter2"', "hunter2"),
        ("Authorization: Bearer abc.def", "abc.def"),
        ("API_KEY: hunter2", "hunter2"),
        ("https://user:hunter2@registry.example.com/x", "hunter2"),
        ("https://discord.com/api/webhooks/123/hunter2", "hunter2"),
    ])
    def test_masks_secrets(self, text, secret):
        assert secret not in redact(text)

    def test_keeps_normal_messages(self):
        text = "Failed to start nginx: port 80 is already allocated"
        assert redact(text) == text

    def test_redacts_nested_values(self):
        assert redact_value({"a": ["password=x"], "b": 1}) == {"a": ["password=[REDACTED]"], "b": 1}


class TestLogBuffer:
    def test_keeps_only_most_recent_lines(self):
        buffer = LogBuffer(size=2)
        for message in ("one", "two", "three"):
            buffer.emit(logging.makeLogRecord({"msg": message}))
        assert buffer.tail() == ["two", "three"]


class TestDescribeContainer:
    def test_never_includes_environment(self):
        described = describe_container(make_container())
        assert described["status"] == "created"
        assert "hunter2" not in str(described)


class TestReporter:
    def send(self, reporter, status=None, side_effect=None):
        reporter._post = AsyncMock(return_value=status, side_effect=side_effect)

        async def run():
            with patch("app.diagnostics.asyncio.sleep", new=AsyncMock()):
                reporter.report("update_failed", "error", "password=hunter2", container="nginx")
                await reporter.drain()

        asyncio.run(run())

    def test_payload_is_redacted(self):
        buffer = LogBuffer()
        buffer.emit(logging.makeLogRecord({"msg": "token=hunter2"}))
        reporter = make_reporter(buffer)
        self.send(reporter, status=201)

        payload = reporter._post.call_args.args[1]
        assert payload["instance_id"] == "a" * 32
        assert payload["container"] == "nginx"
        assert "hunter2" not in str(payload)

    def test_retries_when_server_unreachable(self):
        reporter = make_reporter()
        self.send(reporter, side_effect=OSError("unreachable"))
        assert reporter._post.await_count == 3

    def test_retries_on_server_error(self):
        reporter = make_reporter()
        self.send(reporter, status=503)
        assert reporter._post.await_count == 3

    def test_does_not_retry_rejected_report(self):
        reporter = make_reporter()
        self.send(reporter, status=401)
        assert reporter._post.await_count == 1


    def test_report_from_worker_thread_is_sent(self):
        reporter = make_reporter()
        reporter._post = AsyncMock(return_value=201)

        async def run():
            reporter.report("startup", "info", "started")
            await reporter.drain()
            await asyncio.to_thread(reporter.report, "error_log", "error", "from thread")
            await reporter.drain()

        asyncio.run(run())
        assert reporter._post.await_count == 2

    def test_report_without_loop_is_dropped_safely(self):
        reporter = make_reporter()
        reporter.report("error_log", "error", "no loop yet")

    def test_container_logs_in_report_are_redacted(self):
        reporter = make_reporter()
        reporter._post = AsyncMock(return_value=201)

        async def run():
            reporter.report("update_failed", "error", "x", context={"container": {"logs": ["password=hunter2"]}})
            await reporter.drain()

        asyncio.run(run())
        assert "hunter2" not in str(reporter._post.call_args.args[1])

    def test_heartbeat_loop_posts_identity(self):
        reporter = make_reporter()
        reporter._post = AsyncMock(return_value=200)

        async def run():
            with patch("app.diagnostics.asyncio.sleep", new=AsyncMock(side_effect=[None, asyncio.CancelledError()])):
                with pytest.raises(asyncio.CancelledError):
                    await reporter._heartbeat_loop()

        asyncio.run(run())
        endpoint, payload = reporter._post.call_args.args
        assert endpoint == "https://logs.example.com/api/heartbeat"
        assert payload == {"instance_id": "a" * 32, "instance_name": "host", "version": "1.0.0"}


class TestFitPayload:
    def make(self):
        return {
            "message": "boom",
            "log_lines": ["l" * 1000] * 50,
            "context": {
                "container": {"name": "nginx", "logs": ["c" * 1000] * 50},
                "dependents": [{"name": "a", "logs": ["d" * 1000] * 50} for _ in range(5)],
            },
        }

    def test_small_payload_is_untouched(self):
        payload = {"message": "x", "log_lines": ["y"], "context": {"dependents": [{"logs": ["z"]}]}}
        assert fit_payload(payload)["context"]["dependents"][0]["logs"] == ["z"]

    def test_drops_dependent_logs_first(self):
        payload = fit_payload(self.make())
        assert "logs" not in payload["context"]["dependents"][0]
        assert len(payload["context"]["container"]["logs"]) == 50

    def test_always_ends_under_limit(self):
        payload = self.make()
        payload["context"]["huge"] = "x" * 500_000
        assert len(__import__("json").dumps(fit_payload(payload))) <= 120_000


class TestContainerLogTail:
    def test_decodes_and_truncates(self):
        container = MagicMock()
        container.logs.return_value = b"one\n" + b"x" * 2000 + b"\n\xff"
        lines = container_log_tail(container)
        assert lines[0] == "one"
        assert len(lines[1]) == 1000
        assert len(lines) == 3
        container.logs.assert_called_once_with(tail=50, timestamps=True)


class TestErrorLogForwarder:
    @pytest.fixture
    def setup(self):
        reporter = MagicMock()
        forwarder = ErrorLogForwarder(reporter)
        test_logger = logging.getLogger("app.test_forwarder")
        test_logger.propagate = False
        test_logger.addHandler(forwarder)
        yield reporter, test_logger
        test_logger.removeHandler(forwarder)
        test_logger.propagate = True

    def test_forwards_errors_with_traceback(self, setup):
        reporter, log = setup
        try:
            raise ValueError("bad value")
        except ValueError as e:
            log.error(f"Failed: {e}")
        args, kwargs = reporter.report.call_args
        assert args[:3] == ("error_log", "error", "Failed: bad value")
        assert "ValueError" in kwargs["context"]["traceback"]
        assert "test_forwards_errors_with_traceback" in kwargs["context"]["location"]

    def test_ignores_warnings(self, setup):
        reporter, log = setup
        log.warning("just a warning")
        reporter.report.assert_not_called()

    def test_same_error_is_sent_once(self, setup):
        reporter, log = setup
        log.error("Timeout on 0123456789abcdef after 10s")
        log.error("Timeout on fedcba9876543210 after 20s")
        assert reporter.report.call_count == 1

    def test_different_errors_are_each_sent(self, setup):
        reporter, log = setup
        log.error("first problem")
        log.error("second problem")
        assert reporter.report.call_count == 2

    def test_skips_locally_built_image_noise(self, setup):
        reporter, log = setup
        log.error("Error checking updates for x: 404 pull access denied for x")
        reporter.report.assert_not_called()

    def test_skips_errors_reported_explicitly(self, setup):
        reporter, log = setup
        log.error("Failed to update nginx", extra=ALREADY_REPORTED)
        reporter.report.assert_not_called()

    def test_ignores_unrelated_loggers(self):
        reporter = MagicMock()
        forwarder = ErrorLogForwarder(reporter)
        forwarder.emit(logging.makeLogRecord({"name": "urllib3.connection", "msg": "x", "levelno": 40}))
        reporter.report.assert_not_called()

    def test_reporter_failure_does_not_raise(self, setup):
        reporter, log = setup
        reporter.report.side_effect = RuntimeError("boom")
        logging.raiseExceptions, previous = False, logging.raiseExceptions
        try:
            log.error("still fine")
        finally:
            logging.raiseExceptions = previous


class TestCreateReporter:
    def test_on_by_default(self, monkeypatch):
        monkeypatch.delenv("WHALEKEEPER_DIAGNOSTICS", raising=False)
        monkeypatch.delenv("WHALEKEEPER_DIAGNOSTICS_URL", raising=False)
        db = MagicMock()
        db.get_secure_setting.return_value = "b" * 32
        client = MagicMock()
        client.info.return_value = {"Name": "brother-nas"}

        reporter = create_reporter(db, client, LogBuffer())

        assert reporter.instance_name == "brother-nas"
        assert reporter.endpoint == "https://whalekeeper.rubenpauwels.be/api/events"

    @pytest.mark.parametrize("value", ["off", "OFF", "false", "0", "no"])
    def test_can_be_switched_off(self, monkeypatch, value):
        monkeypatch.setenv("WHALEKEEPER_DIAGNOSTICS", value)
        assert create_reporter(MagicMock(), MagicMock(), LogBuffer()) is None

    def test_instance_id_is_stable(self, temp_db):
        first = get_instance_id(temp_db)
        assert len(first) == 32
        assert get_instance_id(temp_db) == first


class TestMonitorReports:
    def successful_update(self, monitor, dependents):
        container = make_container(status="running")
        monitor.get_container_config = MagicMock(return_value={"name": "nginx"})
        monitor.stop_dependent_containers = MagicMock(return_value=[])
        monitor.restart_dependent_containers = AsyncMock(return_value=dependents)
        monitor.reconnect_networks = MagicMock(return_value=True)
        monitor.monitor_container_health = AsyncMock(return_value=(True, ""))
        monitor.client.containers.run.return_value = make_container(status="running")
        return asyncio.run(monitor.update_container(update_info(container), send_notification=False))

    def test_successful_update_is_reported_as_info(self, monitor):
        assert self.successful_update(monitor, {"qbittorrent": True, "broken": False}) is True

        success = [c for c in monitor.diagnostics.report.call_args_list if c.args[0] == "update_success"]
        assert len(success) == 1
        assert success[0].args[1] == "info"
        context = success[0].kwargs["context"]
        assert context["dependents_restarted"] == ["qbittorrent"]
        assert context["health_check_seconds"] >= 0
        assert "logs" not in str(context)

    def test_failed_update_is_not_reported_as_success(self, monitor):
        container = make_container(status="running")
        monitor.get_container_config = MagicMock(return_value={"name": "nginx"})
        monitor.stop_dependent_containers = MagicMock(return_value=[])
        monitor.client.containers.run.side_effect = docker.errors.APIError("failed")
        monitor.client.containers.get.return_value = make_container()

        asyncio.run(monitor.update_container(update_info(container), send_notification=False))

        kinds = [c.args[0] for c in monitor.diagnostics.report.call_args_list]
        assert "update_success" not in kinds

    def test_successful_compose_update_is_reported(self, monitor, tmp_path):
        compose_file = tmp_path / "compose.yml"
        compose_file.write_text("services: {}")
        container = make_container(status="running")
        container.labels = {"com.docker.compose.project.config_files": str(compose_file)}
        monitor.get_container_config = MagicMock(return_value={"name": "nginx"})
        monitor.stop_dependent_containers = MagicMock(return_value=[])
        monitor.restart_dependent_containers = AsyncMock(return_value={})
        monitor.monitor_container_health = AsyncMock(return_value=(True, ""))
        monitor.client.containers.get.return_value = make_container(status="running")

        with patch("subprocess.run", return_value=MagicMock(returncode=0)), \
                patch("app.docker_monitor.asyncio.sleep", new=AsyncMock()):
            result = asyncio.run(monitor.update_compose_container(update_info(container), "proj", "svc", False))

        assert result is True
        assert monitor.diagnostics.report.call_args.args[:2] == ("update_success", "info")

    def test_self_update_start_is_reported(self, monitor):
        monitor.get_container_config = MagicMock(return_value={"name": "whalekeeper"})
        monitor._build_docker_run_command = MagicMock(return_value="docker run x")
        container = make_container("whalekeeper", status="running")

        assert asyncio.run(monitor.self_update(update_info(container))) is True
        assert monitor.diagnostics.report.call_args.args[:2] == ("self_update_started", "info")

    def test_failed_container_logs_are_included(self, monitor):
        created = make_container(status="exited")
        created.logs.return_value = b"fatal: cannot open database\n"
        monitor.client.containers.get.return_value = created
        assert monitor._capture_state("nginx")["container"]["logs"] == ["fatal: cannot open database"]

    def test_unreadable_logs_do_not_lose_state(self, monitor):
        created = make_container(status="exited")
        created.logs.side_effect = docker.errors.APIError("no logs")
        monitor.client.containers.get.return_value = created
        state = monitor._capture_state("nginx")["container"]
        assert state["status"] == "exited"
        assert "logs" not in state

    def test_report_includes_active_traceback(self, monitor):
        try:
            raise ValueError("bad")
        except ValueError:
            monitor._report("update_failed", "error", "nginx", "bad")
        assert "ValueError" in monitor.diagnostics.report.call_args.kwargs["context"]["traceback"]

    def test_network_reconnect_failure_is_reported(self, monitor):
        container = make_container(status="running")
        monitor.client.containers.get.return_value = container
        monitor.client.networks.get.side_effect = docker.errors.NotFound("gone")
        config = {"name": "nginx", "network_mode": "bridge",
                  "networks": {"app_net": {"aliases": ["web"]}}}

        assert monitor.reconnect_networks(container, config) is False

        args, kwargs = monitor.diagnostics.report.call_args
        assert args[0] == "network_reconnect_failed"
        assert kwargs["context"]["failed_networks"] == {"app_net": "Network not found"}

    def test_null_aliases_do_not_skip_other_networks(self, monitor):
        container = make_container(status="running")
        config = {"name": "nginx", "network_mode": "bridge", "networks": {
            "bridge": {"aliases": None},
            "app_net": {"aliases": None},
        }}

        assert monitor.reconnect_networks(container, config) is True

        monitor.client.networks.get.assert_called_once_with("app_net")
        monitor.diagnostics.report.assert_not_called()

    def test_successful_reconnect_is_not_reported(self, monitor):
        container = make_container(status="running")
        config = {"name": "nginx", "network_mode": "bridge",
                  "networks": {"app_net": {"aliases": ["web"]}}}
        assert monitor.reconnect_networks(container, config) is True
        monitor.diagnostics.report.assert_not_called()

    def test_start_failure_reports_created_container_and_stopped_dependents(self, monitor):
        container = make_container(status="running")
        dependent = make_container("qbittorrent", status="exited")
        created = make_container(status="created", error="port is already allocated")
        monitor.get_container_config = MagicMock(return_value={"name": "nginx"})
        monitor.stop_dependent_containers = MagicMock(return_value=[dependent])
        monitor.client.containers.run.side_effect = docker.errors.APIError("start failed")
        monitor.client.containers.get.return_value = created

        assert asyncio.run(monitor.update_container(update_info(container), send_notification=False)) is False

        args, kwargs = monitor.diagnostics.report.call_args
        assert args[0] == "update_failed"
        assert kwargs["container"] == "nginx"
        assert kwargs["context"]["container"]["error"] == "port is already allocated"
        assert kwargs["context"]["dependents"][0]["name"] == "qbittorrent"

    def test_state_is_captured_before_rollback_removes_container(self, monitor):
        container = make_container(status="running")
        crashed = make_container(status="exited")
        monitor.get_container_config = MagicMock(return_value={"name": "nginx"})
        monitor.stop_dependent_containers = MagicMock(return_value=[])
        monitor.restart_dependent_containers = AsyncMock(return_value={})
        monitor.reconnect_networks = MagicMock(return_value=True)
        monitor.monitor_container_health = AsyncMock(return_value=(False, "crashed"))
        monitor.client.containers.get.return_value = crashed

        async def rollback(*args):
            monitor.client.containers.get.side_effect = docker.errors.NotFound("gone")
            return True
        monitor.rollback_after_failed_update = rollback

        asyncio.run(monitor.update_container(update_info(container), send_notification=False))

        args, kwargs = monitor.diagnostics.report.call_args
        assert args[:2] == ("update_rolled_back", "warning")
        assert kwargs["context"]["container"]["status"] == "exited"
        assert kwargs["context"]["rollback_success"] is True

    def test_failed_dependent_reported_during_batch(self, monitor):
        monitor.client.containers.get.return_value = make_container("qbittorrent", status="exited")
        monitor._report_failed_dependents("gluetun", {"qbittorrent": False, "other": True})

        args, kwargs = monitor.diagnostics.report.call_args
        assert args[0] == "dependent_restart_failed"
        assert "other" not in args[2]

    def test_no_report_when_all_dependents_restart(self, monitor):
        monitor._report_failed_dependents("gluetun", {"qbittorrent": True})
        monitor.diagnostics.report.assert_not_called()

    def test_reporter_failure_does_not_break_update_flow(self, monitor):
        monitor.diagnostics.report.side_effect = RuntimeError("boom")
        monitor._report("update_failed", "error", "nginx", "message")

    def test_no_reporter_is_a_noop(self, monitor):
        monitor.diagnostics = None
        assert monitor._capture_state("nginx") == {}
        monitor._report("update_failed", "error", "nginx", "message")

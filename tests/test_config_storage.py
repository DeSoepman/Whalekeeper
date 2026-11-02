"""Tests for storing the configuration in the database."""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Config, load_config, save_config
from app.docker_monitor import DockerMonitor
from app.web import routes


@pytest.fixture
def legacy(tmp_path):
    return tmp_path / "config.yaml"


def write_yaml(path, data):
    path.write_text(yaml.dump(data))


class TestLoadConfig:
    def test_fresh_install_gets_defaults(self, temp_db, legacy):
        config = load_config(temp_db, str(legacy))
        assert config == Config()
        assert temp_db.get_config_data() == Config().model_dump()

    def test_imports_legacy_file_once(self, temp_db, legacy):
        write_yaml(legacy, {"cron_schedule": "0 2 * * *", "monitoring": {"exclude_containers": ["a", "b"]}})

        config = load_config(temp_db, str(legacy))

        assert config.cron_schedule == "0 2 * * *"
        assert config.monitoring.exclude_containers == ["a", "b"]
        assert config.rollback.keep_versions == 3
        assert not legacy.exists()
        assert (legacy.parent / "config.yaml.migrated").exists()

    def test_database_wins_after_import(self, temp_db, legacy):
        write_yaml(legacy, {"cron_schedule": "0 2 * * *"})
        load_config(temp_db, str(legacy))
        write_yaml(legacy, {"cron_schedule": "0 3 * * *"})

        assert load_config(temp_db, str(legacy)).cron_schedule == "0 2 * * *"

    def test_lost_database_recovers_from_migrated_file(self, temp_db, tmp_path, legacy):
        write_yaml(tmp_path / "config.yaml.migrated", {"monitoring": {"exclude_containers": ["keep-me"]}})

        config = load_config(temp_db, str(legacy))

        assert config.monitoring.exclude_containers == ["keep-me"]

    def test_invalid_file_is_kept_and_stops_startup(self, temp_db, legacy):
        write_yaml(legacy, {"rollback": {"keep_versions": "many"}})

        with pytest.raises(ValueError):
            load_config(temp_db, str(legacy))

        assert legacy.exists()
        assert temp_db.get_config_data() is None

    def test_empty_file_gives_defaults(self, temp_db, legacy):
        legacy.write_text("")
        assert load_config(temp_db, str(legacy)) == Config()

    def test_unrenamable_file_does_not_stop_startup(self, temp_db, legacy, monkeypatch):
        write_yaml(legacy, {"cron_schedule": "0 2 * * *"})
        monkeypatch.setattr("pathlib.Path.rename", Mock(side_effect=OSError("read-only")))

        assert load_config(temp_db, str(legacy)).cron_schedule == "0 2 * * *"
        assert temp_db.get_config_data()["cron_schedule"] == "0 2 * * *"

    def test_password_in_file_moves_to_secure_settings(self, temp_db, legacy):
        write_yaml(legacy, {"notifications": {"email": {"password": "hunter2"}}})

        config = load_config(temp_db, str(legacy))

        assert temp_db.get_secure_setting("smtp_password") == "hunter2"
        assert config.notifications.email.password == ""
        assert "hunter2" not in str(temp_db.get_config_data())

    def test_existing_stored_password_is_not_overwritten(self, temp_db, legacy):
        temp_db.set_secure_setting("smtp_password", "current")
        write_yaml(legacy, {"notifications": {"email": {"password": "old"}}})

        load_config(temp_db, str(legacy))

        assert temp_db.get_secure_setting("smtp_password") == "current"

    def test_example_placeholder_password_is_ignored(self, temp_db, legacy):
        write_yaml(legacy, {"notifications": {"email": {"password": "your-app-password"}}})

        load_config(temp_db, str(legacy))

        assert temp_db.get_secure_setting("smtp_password") is None

    def test_env_overrides_port_without_saving_it(self, temp_db, legacy, monkeypatch):
        monkeypatch.setenv("WHALEKEEPER_WEB_PORT", "6000")
        monkeypatch.setenv("WHALEKEEPER_WEB_HOST", "127.0.0.1")

        config = load_config(temp_db, str(legacy))

        assert (config.web.host, config.web.port) == ("127.0.0.1", 6000)
        assert temp_db.get_config_data()["web"]["port"] == 5454


@pytest.fixture
def api(temp_db, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    save_config(temp_db, Config())
    monkeypatch.setattr(routes, "db", temp_db)
    monitor = Mock()
    monkeypatch.setattr(routes, "monitor", monitor)
    monkeypatch.setattr(routes, "config", Config())
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.require_auth] = lambda: "admin"
    client = TestClient(app)
    client.monitor = monitor
    return client


class TestConfigApi:
    def test_get_returns_full_config(self, api):
        data = api.get("/api/config").json()
        assert data["cron_schedule"] == "0 22 * * 1"
        assert data["notifications"]["webhook"]["method"] == "POST"

    def test_get_masks_stored_password(self, api, temp_db):
        temp_db.set_secure_setting("smtp_password", "hunter2")
        assert api.get("/api/config").json()["notifications"]["email"]["password"] == "********"

    def test_save_round_trip(self, api, temp_db):
        data = api.get("/api/config").json()
        data["cron_schedule"] = "0 4 * * *"

        assert api.post("/api/config", json=data).json()["success"] is True

        assert temp_db.get_config_data()["cron_schedule"] == "0 4 * * *"

    def test_save_keeps_password_out_of_config(self, api, temp_db):
        data = api.get("/api/config").json()
        data["notifications"]["email"]["password"] = "hunter2"

        api.post("/api/config", json=data)

        assert temp_db.get_secure_setting("smtp_password") == "hunter2"
        assert "hunter2" not in str(temp_db.get_config_data())

    def test_masked_password_does_not_overwrite(self, api, temp_db):
        temp_db.set_secure_setting("smtp_password", "hunter2")
        api.post("/api/config", json=api.get("/api/config").json())
        assert temp_db.get_secure_setting("smtp_password") == "hunter2"

    def test_invalid_config_is_rejected_and_not_stored(self, api, temp_db):
        data = api.get("/api/config").json()
        data["rollback"]["keep_versions"] = "many"

        response = api.post("/api/config", json=data)

        assert response.status_code == 400
        assert "keep_versions" in response.json()["detail"]
        assert temp_db.get_config_data()["rollback"]["keep_versions"] == 3

    def test_form_keeps_settings_it_does_not_show(self, api, temp_db):
        save_config(temp_db, Config(
            registry={"username": "me", "password": "secret"},
            web={"host": "127.0.0.1", "port": 5454},
            notifications={"webhook": {"url": "https://a", "headers": {"X-Key": "1"}}},
        ))

        # What the settings form in app.js sends
        response = api.post("/api/config", json={
            "cron_schedule": "0 5 * * *",
            "monitoring": {"auto_restart_dependents": False, "exclude_containers": ["db"]},
            "notifications": {"webhook": {"enabled": True, "url": "https://b"}},
            "web": {"port": 6000},
        })

        assert response.status_code == 200
        stored = temp_db.get_config_data()
        assert stored["registry"] == {"username": "me", "password": "secret"}
        assert stored["web"]["host"] == "127.0.0.1"
        assert stored["web"]["port"] == 6000
        assert stored["notifications"]["webhook"] == {
            "enabled": True, "url": "https://b", "method": "POST", "headers": {"X-Key": "1"},
        }

    def test_lists_are_replaced_not_merged(self, api, temp_db):
        api.post("/api/config", json={"monitoring": {"exclude_containers": ["a", "b"]}})
        api.post("/api/config", json={"monitoring": {"exclude_containers": ["b"]}})
        assert temp_db.get_config_data()["monitoring"]["exclude_containers"] == ["b"]

    def test_toggle_monitoring(self, api, temp_db):
        api.post("/api/toggle-monitoring/nginx", json={"enabled": False})
        assert temp_db.get_config_data()["monitoring"]["exclude_containers"] == ["nginx"]
        applied = api.monitor.apply_config.call_args.args[0]
        assert applied.monitoring.exclude_containers == ["nginx"]

        api.post("/api/toggle-monitoring/nginx", json={"enabled": True})
        assert temp_db.get_config_data()["monitoring"]["exclude_containers"] == []

    def test_complete_setup(self, api, temp_db):
        temp_db.create_user("admin", "hash")
        response = api.post(
            "/api/complete-setup",
            json={"cron_schedule": "0 1 * * *", "self_update": "notify", "exclude_containers": ["db"]},
        )
        assert response.json()["success"] is True
        stored = temp_db.get_config_data()
        assert stored["cron_schedule"] == "0 1 * * *"
        assert stored["monitoring"]["exclude_containers"] == ["whalekeeper", "db"]
        assert temp_db.get_user("admin")["setup_completed"]

    def test_save_applies_config_immediately(self, api):
        response = api.post("/api/config", json={"monitoring": {"exclude_containers": ["db"]}}).json()

        assert response["restart_required"] is False
        assert "applied" in response["message"]
        assert api.monitor.apply_config.call_args.args[0].monitoring.exclude_containers == ["db"]
        assert routes.config.monitoring.exclude_containers == ["db"]

    @pytest.mark.parametrize("web", [{"port": 6000}, {"host": "127.0.0.1"}])
    def test_changing_host_or_port_asks_for_restart(self, api, web):
        response = api.post("/api/config", json={"web": web}).json()
        assert response["restart_required"] is True
        assert "Restart" in response["message"]

    def test_resending_same_port_does_not_ask_for_restart(self, api):
        assert api.post("/api/config", json={"web": {"port": 5454}}).json()["restart_required"] is False

    def test_invalid_schedule_is_rejected(self, api, temp_db):
        response = api.post("/api/config", json={"cron_schedule": "every day"})
        assert response.status_code == 400
        assert temp_db.get_config_data()["cron_schedule"] == "0 22 * * 1"

    def test_empty_schedule_is_allowed(self, api, temp_db):
        assert api.post("/api/config", json={"cron_schedule": ""}).status_code == 200
        assert temp_db.get_config_data()["cron_schedule"] == ""

    def test_wizard_rejects_invalid_schedule(self, api, temp_db):
        temp_db.create_user("admin", "hash")
        assert api.post("/api/complete-setup", json={"cron_schedule": "nope"}).status_code == 400

    def test_env_override_is_not_saved_by_reload(self, api, temp_db, monkeypatch):
        monkeypatch.setenv("WHALEKEEPER_WEB_PORT", "6000")
        api.post("/api/toggle-monitoring/nginx", json={"enabled": False})
        assert temp_db.get_config_data()["web"]["port"] == 5454


class TestApplyConfig:
    @pytest.fixture
    def monitor(self, test_config):
        with patch("app.docker_monitor.docker.from_env"):
            instance = DockerMonitor(test_config, Mock(), Mock())
        instance.self_check_loop = AsyncMock()
        instance.check_all_containers = AsyncMock()
        return instance

    def test_updates_monitor_and_notifier(self, monitor):
        new = Config(rollback={"keep_versions": 9})
        monitor.apply_config(new)
        assert monitor.config is new
        assert monitor.notifier.config is new

    def test_same_schedule_does_not_wake_the_loop(self, monitor):
        monitor.apply_config(Config(cron_schedule=monitor.config.cron_schedule))
        assert not monitor.schedule_changed.is_set()

    def test_new_schedule_is_used_without_restart(self, monitor):
        async def run():
            task = asyncio.create_task(monitor.start_monitoring())
            await asyncio.sleep(0.05)
            monitor.check_all_containers.assert_not_awaited()

            # Replaces a schedule that is months away by one due right now
            with patch("app.docker_monitor.croniter") as cron:
                cron.return_value.get_next.side_effect = lambda _: datetime.now() + timedelta(milliseconds=10)
                monitor.apply_config(Config(cron_schedule="* * * * *"))
                await asyncio.sleep(0.2)
            monitor.running = False
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        monitor.config = Config(cron_schedule="0 0 1 1 *")
        asyncio.run(run())
        assert monitor.check_all_containers.await_count >= 1

    def test_empty_schedule_waits_until_one_is_configured(self, monitor):
        async def run():
            task = asyncio.create_task(monitor.start_monitoring())
            await asyncio.sleep(0.05)
            monitor.check_all_containers.assert_not_awaited()
            with patch("app.docker_monitor.croniter") as cron:
                cron.return_value.get_next.side_effect = lambda _: datetime.now() + timedelta(milliseconds=10)
                monitor.apply_config(Config(cron_schedule="* * * * *"))
                await asyncio.sleep(0.2)
            monitor.running = False
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        monitor.config = Config(cron_schedule="")
        asyncio.run(run())
        assert monitor.check_all_containers.await_count >= 1

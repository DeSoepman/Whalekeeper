"""Tests for storing the configuration in the database."""
from unittest.mock import Mock

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Config, load_config, save_config
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
        assert api.monitor.config.monitoring.exclude_containers == ["nginx"]

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

    def test_env_override_is_not_saved_by_reload(self, api, temp_db, monkeypatch):
        monkeypatch.setenv("WHALEKEEPER_WEB_PORT", "6000")
        api.post("/api/toggle-monitoring/nginx", json={"enabled": False})
        assert temp_db.get_config_data()["web"]["port"] == 5454

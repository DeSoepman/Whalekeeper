import logging
import os
import yaml
from pathlib import Path
from typing import Dict, List, Optional
from pydantic import BaseModel
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

# Older versions kept the configuration here. It is imported once, see load_config.
LEGACY_CONFIG_PATH = "config/config.yaml"
MIGRATED_SUFFIX = ".migrated"
# Placeholder in the config.example.yaml that older versions copied on first start.
EXAMPLE_SMTP_PASSWORD = "your-app-password"


class MonitoringConfig(BaseModel):
    exclude_containers: List[str] = []
    auto_restart_dependents: bool = True  # Restart containers that depend on updated containers


class EmailConfig(BaseModel):
    enabled: bool = False
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    use_tls: bool = True
    username: str = ""
    password: str = ""
    from_address: str = ""
    to_addresses: List[str] = []
    # Notification preferences
    notify_on_batch_complete: bool = True
    notify_on_rollback: bool = True


class DiscordConfig(BaseModel):
    enabled: bool = False
    webhook_url: str = ""


class WebhookConfig(BaseModel):
    enabled: bool = False
    url: str = ""
    method: str = "POST"
    headers: Dict[str, str] = {}


class NotificationsConfig(BaseModel):
    email: EmailConfig = EmailConfig()
    discord: DiscordConfig = DiscordConfig()
    webhook: WebhookConfig = WebhookConfig()


class RollbackConfig(BaseModel):
    keep_versions: int = 3


class WebConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 5454
    username: str = ""
    password: str = ""


class RegistryConfig(BaseModel):
    username: str = ""
    password: str = ""


class Config(BaseModel):
    cron_schedule: str = "0 22 * * 1"  # Every Monday at 10 PM
    monitoring: MonitoringConfig = MonitoringConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    rollback: RollbackConfig = RollbackConfig()
    web: WebConfig = WebConfig()
    registry: RegistryConfig = RegistryConfig()


def import_legacy_config(db, legacy_path: str = LEGACY_CONFIG_PATH) -> Optional[Dict]:
    """
    Read the config.yaml of an older version, once. Returns None when there is none.

    The renamed copy is read as well: if the database got lost (no volume on ./data),
    falling back to defaults would silently drop the user's excluded containers.
    """
    legacy_file = Path(legacy_path)
    migrated_file = Path(f"{legacy_path}{MIGRATED_SUFFIX}")
    source = legacy_file if legacy_file.exists() else migrated_file
    if not source.exists():
        return None

    with open(source, 'r') as f:
        data = yaml.safe_load(f) or {}

    # Fails on an invalid file rather than continuing with defaults, as before.
    Config(**data)

    email = (data.get('notifications') or {}).get('email') or {}
    password = email.get('password')
    if password:
        if password != EXAMPLE_SMTP_PASSWORD and not db.get_secure_setting("smtp_password"):
            db.set_secure_setting("smtp_password", password)
        email['password'] = ''

    logger.info(f"Imported configuration from {source}")
    return data


def retire_legacy_config(legacy_path: str = LEGACY_CONFIG_PATH) -> None:
    """Rename config.yaml, so nobody edits a file that no longer has any effect."""
    legacy_file = Path(legacy_path)
    if not legacy_file.exists():
        return
    try:
        legacy_file.rename(f"{legacy_path}{MIGRATED_SUFFIX}")
    except OSError as e:
        logger.warning(f"Could not rename {legacy_path}, it is no longer used: {e}")


def load_config(db, legacy_path: str = LEGACY_CONFIG_PATH) -> Config:
    """Load the configuration from the database, importing a config.yaml on first start."""
    data = db.get_config_data()
    if data is not None:
        return apply_env_overrides(Config(**data))

    config = Config(**(import_legacy_config(db, legacy_path) or {}))
    save_config(db, config)
    # Only after saving: a crash in between must not lose the configuration.
    retire_legacy_config(legacy_path)
    return apply_env_overrides(config)


def merge_config(current: Dict, updates: Dict) -> Dict:
    """
    Apply updates on top of the stored configuration. Forms only send the settings they
    show, so anything else (like registry credentials) must keep its value.
    """
    merged = dict(current)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def save_config(db, config: Config) -> None:
    db.save_config_data(config.model_dump())


def apply_env_overrides(config: Config) -> Config:
    """
    Environment variables win over stored values, and are never saved. They are the
    way back in when a wrong host or port makes the web interface unreachable.
    """
    host = os.environ.get("WHALEKEEPER_WEB_HOST")
    port = os.environ.get("WHALEKEEPER_WEB_PORT")
    if host:
        config.web.host = host
    if port:
        config.web.port = int(port)
    return config

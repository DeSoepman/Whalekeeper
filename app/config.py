import yaml
from pathlib import Path
from typing import Dict, List, Optional
from pydantic import BaseModel
from pydantic_settings import BaseSettings


class MonitoringConfig(BaseModel):
    monitor_all: bool = True
    labels: Optional[List[str]] = None
    exclude_containers: List[str] = []


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
    notify_on_update_found: bool = True
    notify_on_no_updates: bool = False
    notify_on_success: bool = True
    notify_on_error: bool = True


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


def load_config(config_path: str = "config/config.yaml") -> Config:
    """Load configuration from YAML file"""
    config_file = Path(config_path)
    
    if not config_file.exists():
        # Try example config
        example_config = Path("config/config.example.yaml")
        if example_config.exists():
            print(f"Warning: {config_path} not found, using example config")
            config_file = example_config
        else:
            print(f"Warning: No config file found, using defaults")
            return Config()
    
    with open(config_file, 'r') as f:
        config_data = yaml.safe_load(f)
    
    return Config(**config_data)

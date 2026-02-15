import yaml
from pathlib import Path
from typing import Dict, List, Optional
from pydantic import BaseModel
from pydantic_settings import BaseSettings


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


def migrate_config(config_path: str = "config/config.yaml") -> bool:
    """
    Migrate config file to ensure all expected keys exist with default values.
    Returns True if config was updated, False otherwise.
    """
    config_file = Path(config_path)
    
    if not config_file.exists():
        return False
    
    try:
        with open(config_file, 'r') as f:
            config_data = yaml.safe_load(f) or {}
        
        updated = False
        
        # Ensure monitoring section exists
        if 'monitoring' not in config_data:
            config_data['monitoring'] = {}
            updated = True
        
        if 'auto_restart_dependents' not in config_data.get('monitoring', {}):
            config_data['monitoring']['auto_restart_dependents'] = True
            print("Added missing config key: monitoring.auto_restart_dependents = True")
            updated = True
        
        if 'exclude_containers' not in config_data.get('monitoring', {}):
            config_data['monitoring']['exclude_containers'] = []
            updated = True
        
        # Ensure web section has host
        if 'web' not in config_data:
            config_data['web'] = {}
            updated = True
        
        if 'host' not in config_data.get('web', {}):
            config_data['web']['host'] = '0.0.0.0'
            print("Added missing config key: web.host = 0.0.0.0")
            updated = True
        
        # Ensure webhook has method and headers
        if 'notifications' in config_data and 'webhook' in config_data['notifications']:
            webhook = config_data['notifications']['webhook']
            if 'method' not in webhook:
                webhook['method'] = 'POST'
                updated = True
            if 'headers' not in webhook:
                webhook['headers'] = {}
                updated = True
        
        # Write back if updated
        if updated:
            with open(config_file, 'w') as f:
                yaml.dump(config_data, f, default_flow_style=False, sort_keys=False)
            print(f"Config file migrated: {config_path}")
        
        return updated
        
    except Exception as e:
        print(f"Error migrating config: {e}")
        return False


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
    
    # Migrate config to add any missing keys
    migrate_config(config_path)
    
    with open(config_file, 'r') as f:
        config_data = yaml.safe_load(f)
    
    return Config(**config_data)

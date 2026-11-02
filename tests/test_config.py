import pytest
from app.config import Config


@pytest.mark.unit
def test_default_config():
    """Test default configuration values"""
    config = Config()
    
    assert config.cron_schedule == "0 22 * * 1"
    assert config.rollback.keep_versions == 3
    assert config.web.port == 5454
    assert config.notifications.email.enabled is False


@pytest.mark.unit
def test_config_from_dict():
    """Test creating config from dictionary"""
    config_dict = {
        "cron_schedule": "0 */6 * * *",
        "monitoring": {
            "exclude_containers": ["whalekeeper", "test"]
        },
        "rollback": {
            "keep_versions": 5
        }
    }
    
    config = Config(**config_dict)
    assert config.cron_schedule == "0 */6 * * *"
    assert "whalekeeper" in config.monitoring.exclude_containers
    assert "test" in config.monitoring.exclude_containers
    assert config.rollback.keep_versions == 5


@pytest.mark.unit
def test_email_config():
    """Test email notification configuration"""
    config_dict = {
        "notifications": {
            "email": {
                "enabled": True,
                "smtp_host": "smtp.gmail.com",
                "smtp_port": 587,
                "username": "test@example.com",
                "from_address": "test@example.com",
                "to_addresses": ["recipient@example.com"]
            }
        }
    }
    
    config = Config(**config_dict)
    assert config.notifications.email.enabled is True
    assert config.notifications.email.smtp_host == "smtp.gmail.com"
    assert config.notifications.email.smtp_port == 587
    assert "recipient@example.com" in config.notifications.email.to_addresses


@pytest.mark.unit
def test_discord_config():
    """Test Discord notification configuration"""
    config_dict = {
        "notifications": {
            "discord": {
                "enabled": True,
                "webhook_url": "https://discord.com/api/webhooks/123/abc"
            }
        }
    }
    
    config = Config(**config_dict)
    assert config.notifications.discord.enabled is True
    assert config.notifications.discord.webhook_url.startswith("https://discord.com")


@pytest.mark.unit
def test_registry_config():
    """Test Docker registry configuration"""
    config_dict = {
        "registry": {
            "username": "dockeruser",
            "password": "dockerpass"
        }
    }
    
    config = Config(**config_dict)
    assert config.registry.username == "dockeruser"
    assert config.registry.password == "dockerpass"

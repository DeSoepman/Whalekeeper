import pytest
import aiohttp
from unittest.mock import AsyncMock, patch, MagicMock
from app.notifications import NotificationService


@pytest.mark.unit
def test_notifier_init(test_config):
    """Test notification service initialization"""
    notifier = NotificationService(test_config)
    assert notifier.config == test_config


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_email_disabled(test_config, temp_db):
    """Test email notification when disabled"""
    notifier = NotificationService(test_config, temp_db)
    
    # Should not raise error when disabled
    with patch.object(notifier, 'send_email') as mock_send:
        await notifier.send_notification(
            "Test Title",
            "Test Message",
            notification_type="success"
        )
        # send_email should not be called when disabled
        mock_send.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_discord_disabled(test_config, temp_db):
    """Test Discord notification when disabled"""
    notifier = NotificationService(test_config, temp_db)
    
    with patch.object(notifier, 'send_discord', new_callable=AsyncMock) as mock_send:
        await notifier.send_notification(
            "Test Title",
            "Test Message",
            notification_type="success"
        )
        # send_discord should not be called when disabled
        mock_send.assert_not_called()


@pytest.mark.unit
def test_format_update_info_html(test_config):
    """Test HTML formatting of update info"""
    notifier = NotificationService(test_config)
    
    update_info = {
        "Container": "nginx",
        "Old Image": "nginx:1.25",
        "New Image": "nginx:1.26",
        "Status": "Success"
    }
    
    html = notifier._format_update_info_html(update_info)
    
    assert "nginx" in html
    assert "nginx:1.25" in html
    assert "nginx:1.26" in html
    assert "Success" in html


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_discord_webhook(test_config, temp_db):
    """Test sending Discord webhook"""
    # Enable Discord
    test_config.notifications.discord.enabled = True
    test_config.notifications.discord.webhook_url = "https://discord.com/api/webhooks/test"
    
    notifier = NotificationService(test_config, temp_db)
    
    with patch('aiohttp.ClientSession') as mock_session_class:
        mock_session = MagicMock()
        mock_session_class.return_value.__aenter__.return_value = mock_session
        mock_session_class.return_value.__aexit__.return_value = AsyncMock()
        
        mock_response = MagicMock()
        mock_response.status = 204
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock()
        
        mock_session.post.return_value = mock_response
        
        await notifier.send_discord(
            "Test Title",
            "Test Message",
            {"Container": "nginx"}
        )
        
        # Verify webhook was called
        mock_session.post.assert_called_once()

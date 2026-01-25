#!/usr/bin/env python3
"""Test script to send a compose rollback notification email"""

import asyncio
import sys
sys.path.insert(0, '/home/ruben/docker-updates')

from app.config import load_config
from app.database import Database
from app.notifications import NotificationService


async def test_rollback_email():
    print("Loading configuration...")
    config = load_config()
    db = Database()
    notifier = NotificationService(config, db)
    
    # Check if email is enabled
    if not config.notifications.email.enabled:
        print("ERROR: Email notifications are disabled in config.yaml")
        print("Enable them first: notifications.email.enabled: true")
        return
    
    print(f"Sending test email to: {', '.join(config.notifications.email.to_addresses)}")
    
    # Simulate the compose rollback notification
    message = (
        "The update failed health checks and was automatically rolled back.\n\n"
        "Your service is running with the old version as a STANDALONE container "
        "(no longer managed by docker-compose).\n\n"
        "To restore compose management:\n"
        "1. Stop and remove the standalone container:\n"
        "   docker stop test-container\n"
        "   docker rm test-container\n\n"
        "2. Go to your compose directory and start the service:\n"
        "   cd /home/user/myapp\n"
        "   docker compose up -d myservice\n\n"
        "This will restore full compose management with networks and dependencies."
    )
    
    await notifier.send_notification(
        title="⚠️ Auto-Rollback (Compose): test-container",
        message=message,
        update_info={
            "Container": "test-container",
            "Old Image": "myapp:1.0",
            "New Image (Failed)": "myapp:2.0",
            "Failure Reason": "Container stopped (status: exited, exit code: 1)",
            "Rollback Status": "Success - Running as standalone",
            "Original Project": "myproject",
            "Original Service": "myservice"
        },
        notification_type="warning"
    )
    
    print("✓ Test rollback email sent successfully!")
    print(f"Check your inbox: {', '.join(config.notifications.email.to_addresses)}")


if __name__ == "__main__":
    try:
        asyncio.run(test_rollback_email())
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()

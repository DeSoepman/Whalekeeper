import smtplib
import aiohttp
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict
import logging

from app.config import Config
from app.database import Database

logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(self, config: Config, db: Database = None):
        self.config = config
        self.db = db
    
    async def send_notification(self, title: str, message: str, 
                               update_info: Dict = None, notification_type: str = "update"):
        """Send notifications via all enabled channels
        
        notification_type can be: 'update_found', 'no_updates', 'success', 'error'
        Note: Email preferences are now checked at the source (batch/rollback calls)
        """
        
        # Send email if enabled (preferences already checked by caller)
        if self.config.notifications.email.enabled:
            try:
                self.send_email(title, message, update_info)
            except Exception as e:
                logger.error(f"Email notification failed: {e}")
        
        if self.config.notifications.discord.enabled:
            try:
                await self.send_discord(title, message, update_info)
            except Exception as e:
                logger.error(f"Discord notification failed: {e}")
        
        if self.config.notifications.webhook.enabled:
            try:
                await self.send_webhook(title, message, update_info)
            except Exception as e:
                logger.error(f"Webhook notification failed: {e}")
    
    def send_email(self, title: str, message: str, update_info: Dict = None):
        """Send email notification via SMTP"""
        email_config = self.config.notifications.email
        
        msg = MIMEMultipart('alternative')
        msg['From'] = email_config.from_address
        msg['To'] = ', '.join(email_config.to_addresses)
        msg['Subject'] = f"Whalekeeper: {title}"
        
        # Plain text version
        text_body = f"{message}\n\n"
        if update_info:
            text_body += "Update Details:\n"
            for key, value in update_info.items():
                text_body += f"  {key}: {value}\n"
        
        # HTML version with light theme
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
        </head>
        <body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif; background: #f5f5f5; padding: 40px 20px;">
            <div style="max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 10px; overflow: hidden; border: 1px solid #e0e0e0; box-shadow: 0 2px 8px rgba(0,0,0,0.05);">
                <!-- Header -->
                <div style="padding: 40px 30px; border-bottom: 1px solid #e0e0e0; text-align: center;">
                    <div style="font-size: 40px; margin-bottom: 10px;">🐳</div>
                    <h1 style="margin: 0; color: #1a1a1a; font-size: 24px; font-weight: 600;">Whalekeeper</h1>
                    <p style="margin: 10px 0 0 0; color: #22c55e; font-size: 16px; font-weight: 600;">{title}</p>
                </div>
                
                <!-- Content -->
                <div style="padding: 40px 30px;">
                    <div style="background: #fafafa; padding: 20px; border-radius: 8px; margin-bottom: 25px; border: 1px solid #e5e5e5;">
                        <pre style="margin: 0; color: #1a1a1a; line-height: 1.8; font-size: 14px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; white-space: pre-wrap; word-wrap: break-word;">{message}</pre>
                    </div>
                    
                    {self._format_update_info_html(update_info) if update_info else ''}
                </div>
                
                <!-- Footer -->
                <div style="padding: 20px 30px; background: #fafafa; border-top: 1px solid #e5e5e5; text-align: center;">
                    <p style="margin: 0; color: #999999; font-size: 12px;">
                        Automated notification from Whalekeeper
                    </p>
                </div>
            </div>
        </body>
        </html>
        """
        
        # Attach both versions
        part1 = MIMEText(text_body, 'plain')
        part2 = MIMEText(html_body, 'html')
        msg.attach(part1)
        msg.attach(part2)
        
        with smtplib.SMTP(email_config.smtp_host, email_config.smtp_port) as server:
            if email_config.use_tls:
                server.starttls()
            
            # Get password from database if available, otherwise fallback to config
            password = None
            if self.db:
                password = self.db.get_secure_setting("smtp_password")
            if not password:
                password = email_config.password
            
            if email_config.username and password:
                server.login(email_config.username, password)
            server.send_message(msg)
        
        logger.info(f"Email sent: {title}")
    
    def _format_update_info_html(self, update_info: Dict) -> str:
        """Format update info as HTML table"""
        if not update_info:
            return ""
        
        rows = ""
        for key, value in update_info.items():
            rows += f"""
            <tr style="border-bottom: 1px solid #e5e5e5;">
                <td style="padding: 12px 15px; color: #666666; font-size: 13px; font-weight: 600;">{key}</td>
                <td style="padding: 12px 15px; color: #1a1a1a; font-size: 13px;">{value}</td>
            </tr>
            """
        
        return f"""
        <h3 style="color: #1a1a1a; font-size: 16px; margin: 0 0 15px 0; font-weight: 600;">Update Details</h3>
        <table style="width: 100%; border-collapse: collapse; background: #fafafa; border-radius: 8px; overflow: hidden; border: 1px solid #e5e5e5;">
            {rows}
        </table>
        """
    
    def send_test_email(self, smtp_host: str, smtp_port: int, use_tls: bool,
                       username: str, password: str, from_address: str,
                       to_addresses: list) -> Dict:
        """Send a test email to verify SMTP settings"""
        try:
            msg = MIMEMultipart('alternative')
            msg['From'] = from_address
            msg['To'] = ', '.join(to_addresses)
            msg['Subject'] = "🐳 Whalekeeper - Test Email"
            
            # Plain text version
            text_body = "This is a test email from Whalekeeper.\n\n"
            text_body += "If you receive this message, your SMTP settings are configured correctly!\n\n"
            text_body += f"SMTP Server: {smtp_host}:{smtp_port}\n"
            text_body += f"TLS Enabled: {use_tls}\n"
            text_body += f"From: {from_address}\n"
            text_body += f"To: {', '.join(to_addresses)}\n"
            
            # HTML version with modern styling
            html_body = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
            </head>
            <body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif; background: #f5f5f5; padding: 40px 20px;">
                <div style="max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 10px; overflow: hidden; border: 1px solid #e0e0e0; box-shadow: 0 2px 8px rgba(0,0,0,0.05);">
                    <!-- Header -->
                    <div style="padding: 40px 30px; border-bottom: 1px solid #e0e0e0; text-align: center;">
                        <div style="font-size: 40px; margin-bottom: 10px;">🐳</div>
                        <h1 style="margin: 0; color: #1a1a1a; font-size: 24px; font-weight: 600;">Whalekeeper</h1>
                        <p style="margin: 10px 0 0 0; color: #666666; font-size: 14px;">Docker Container Update Monitor</p>
                    </div>
                    
                    <!-- Content -->
                    <div style="padding: 40px 30px;">
                        <div style="background: #f0fdf4; border: 1px solid #86efac; border-left: 3px solid #22c55e; padding: 20px; border-radius: 8px; margin-bottom: 30px;">
                            <h2 style="margin: 0 0 10px 0; color: #16a34a; font-size: 18px; font-weight: 600;">✓ Test Email Successful</h2>
                            <p style="margin: 0; color: #166534; line-height: 1.6; font-size: 14px;">
                                Your SMTP settings are configured correctly and working as expected.
                            </p>
                        </div>
                        
                        <h3 style="color: #1a1a1a; font-size: 16px; margin: 0 0 15px 0; font-weight: 600;">Configuration Details</h3>
                        
                        <table style="width: 100%; border-collapse: collapse; background: #fafafa; border-radius: 8px; overflow: hidden; border: 1px solid #e5e5e5;">
                            <tr style="border-bottom: 1px solid #e5e5e5;">
                                <td style="padding: 15px 20px; color: #666666; font-size: 14px; width: 40%;">SMTP Server</td>
                                <td style="padding: 15px 20px; color: #1a1a1a; font-size: 14px; font-family: 'Courier New', monospace;">{smtp_host}:{smtp_port}</td>
                            </tr>
                            <tr style="border-bottom: 1px solid #e5e5e5;">
                                <td style="padding: 15px 20px; color: #666666; font-size: 14px;">TLS Enabled</td>
                                <td style="padding: 15px 20px; color: #1a1a1a; font-size: 14px;">{'Yes' if use_tls else 'No'}</td>
                            </tr>
                            <tr style="border-bottom: 1px solid #e5e5e5;">
                                <td style="padding: 15px 20px; color: #666666; font-size: 14px;">From Address</td>
                                <td style="padding: 15px 20px; color: #1a1a1a; font-size: 14px; font-family: 'Courier New', monospace;">{from_address}</td>
                            </tr>
                            <tr>
                                <td style="padding: 15px 20px; color: #666666; font-size: 14px;">Recipients</td>
                                <td style="padding: 15px 20px; color: #1a1a1a; font-size: 14px; font-family: 'Courier New', monospace;">{', '.join(to_addresses)}</td>
                            </tr>
                        </table>
                    </div>
                    
                    <!-- Footer -->
                    <div style="padding: 20px 30px; background: #fafafa; border-top: 1px solid #e5e5e5; text-align: center;">
                        <p style="margin: 0; color: #999999; font-size: 12px;">
                            Automated test message from Whalekeeper
                        </p>
                    </div>
                </div>
            </body>
            </html>
            """
            
            # Attach both versions
            part1 = MIMEText(text_body, 'plain')
            part2 = MIMEText(html_body, 'html')
            msg.attach(part1)
            msg.attach(part2)
            
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                if use_tls:
                    server.starttls()
                if username and password:
                    server.login(username, password)
                server.send_message(msg)
            
            logger.info("Test email sent successfully")
            return {"success": True, "message": "Test email sent successfully!"}
            
        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP authentication failed: {e}")
            return {"success": False, "message": f"Authentication failed: {str(e)}"}
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error: {e}")
            return {"success": False, "message": f"SMTP error: {str(e)}"}
        except Exception as e:
            logger.error(f"Failed to send test email: {e}")
            return {"success": False, "message": f"Error: {str(e)}"}
    
    async def send_discord(self, title: str, message: str, 
                          update_info: Dict = None):
        """Send Discord webhook notification"""
        discord_config = self.config.notifications.discord
        
        embed = {
            "title": title,
            "description": message,
            "color": 3447003,  # Blue
            "timestamp": None
        }
        
        if update_info:
            embed["fields"] = [
                {"name": key, "value": str(value), "inline": True}
                for key, value in update_info.items()
            ]
        
        payload = {"embeds": [embed]}
        
        async with aiohttp.ClientSession() as session:
            async with session.post(discord_config.webhook_url, 
                                   json=payload) as response:
                if response.status == 204:
                    logger.info(f"Discord notification sent: {title}")
                else:
                    logger.error(f"Discord notification failed: {response.status}")
    
    async def send_webhook(self, title: str, message: str, 
                          update_info: Dict = None):
        """Send generic webhook notification"""
        webhook_config = self.config.notifications.webhook
        
        payload = {
            "title": title,
            "message": message,
            "update_info": update_info or {}
        }
        
        async with aiohttp.ClientSession() as session:
            method = webhook_config.method.upper()
            async with session.request(
                method, 
                webhook_config.url,
                json=payload,
                headers=webhook_config.headers
            ) as response:
                if 200 <= response.status < 300:
                    logger.info(f"Webhook notification sent: {title}")
                else:
                    logger.error(f"Webhook notification failed: {response.status}")

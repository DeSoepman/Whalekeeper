from fastapi import APIRouter, HTTPException, Response, Cookie, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from typing import List, Dict, Optional
import logging
import yaml
from pathlib import Path
import os
import secrets
from itsdangerous import URLSafeTimedSerializer, BadSignature
from passlib.context import CryptContext

from app.docker_monitor import DockerMonitor
from app.database import Database
from app.config import Config

logger = logging.getLogger(__name__)

router = APIRouter()

# Setup templates
templates = Jinja2Templates(directory="app/web/templates")

# Session management
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
session_serializer = URLSafeTimedSerializer(SECRET_KEY)

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Will be set by main.py
monitor: DockerMonitor = None
db: Database = None
config: Config = None


def get_session_cookie(session: Optional[str] = Cookie(None)) -> Optional[str]:
    """Get and validate session cookie"""
    if not session:
        return None
    try:
        # Validate session (max age 30 days for remember me, 24 hours otherwise)
        data = session_serializer.loads(session, max_age=30*24*60*60)
        return data
    except BadSignature:
        return None


def require_auth(session_data: Optional[str] = Depends(get_session_cookie)):
    """Dependency to require authentication"""
    if not session_data:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return session_data


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    """Serve the registration page"""
    # Only show registration if no users exist
    if db.has_users():
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("register.html", {"request": request})


@router.post("/api/register")
async def register(request: Request):
    """Handle registration"""
    try:
        # Only allow registration if no users exist
        if db.has_users():
            return {"success": False, "message": "Registration is disabled"}
        
        data = await request.json()
        username = data.get("username", "").strip()
        password = data.get("password", "")
        
        # Validation
        if len(username) < 3:
            return {"success": False, "message": "Username must be at least 3 characters"}
        
        if len(password) < 8:
            return {"success": False, "message": "Password must be at least 8 characters"}
        
        # Hash password and create user
        password_hash = pwd_context.hash(password)
        success = db.create_user(username, password_hash)
        
        if success:
            return {"success": True, "message": "Account created successfully"}
        else:
            return {"success": False, "message": "Username already exists"}
            
    except Exception as e:
        logger.error(f"Registration error: {e}")
        return {"success": False, "message": "Registration failed"}


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Serve the login page"""
    # Redirect to registration if no users exist
    if not db.has_users():
        return RedirectResponse(url="/register", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@router.post("/api/login")
async def login(request: Request, response: Response):
    """Handle login"""
    try:
        data = await request.json()
        username = data.get("username")
        password = data.get("password")
        remember = data.get("remember", False)
        
        # Get user from database
        user = db.get_user(username)
        
        # Validate credentials
        if user and pwd_context.verify(password, user['password_hash']):
            # Create session token
            session_token = session_serializer.dumps({"username": username})
            
            # Set cookie (30 days if remember me, session otherwise)
            max_age = 30*24*60*60 if remember else None
            response.set_cookie(
                key="session",
                value=session_token,
                httponly=True,
                max_age=max_age,
                samesite="lax"
            )
            
            return {"success": True}
        else:
            return {"success": False, "message": "Invalid username or password"}
    except Exception as e:
        logger.error(f"Login error: {e}")
        return {"success": False, "message": "Login failed"}


@router.post("/api/logout")
async def logout(response: Response):
    """Handle logout"""
    response.delete_cookie("session")
    return {"success": True}


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, session: Optional[str] = Cookie(None)):
    """Serve the web GUI"""
    # Check if setup is needed (no users exist)
    if not db.has_users():
        return RedirectResponse(url="/register", status_code=302)
    
    # Check if user is authenticated
    session_data = get_session_cookie(session)
    if not session_data:
        # Redirect to login page if not authenticated
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("index.html", {"request": request})



@router.get("/api/version")
async def get_version():
    """Get application version (public endpoint)"""
    try:
        version_file = Path("VERSION")
        if version_file.exists():
            version = version_file.read_text().strip()
            return {"version": version}
        return {"version": "1.0.0"}
    except Exception as e:
        logger.error(f"Error reading version: {e}")
        return {"version": "1.0.0"}


@router.get("/api/containers")
async def get_containers(session_data: str = Depends(require_auth)):
    """Get list of monitored containers"""
    try:
        # Get all containers, not just monitored ones
        all_containers = monitor.client.containers.list()
        exclude_list = config.monitoring.exclude_containers
        
        containers = []
        for c in all_containers:
            # Extract version from image labels
            version = None
            if c.image.labels:
                version = (
                    c.image.labels.get('io.hass.version') or
                    c.image.labels.get('org.opencontainers.image.version') or
                    c.image.labels.get('version') or
                    c.image.labels.get('VERSION')
                )
            
            containers.append({
                "name": c.name,
                "id": c.id[:12],
                "image": c.image.tags[0] if c.image.tags else c.image.id[:12],
                "status": c.status,
                "monitored": c.name not in exclude_list,
                "version": version,
                "has_update": monitor.has_update(c.name),
                "monitoring_active": bool(config.cron_schedule and config.cron_schedule.strip())
            })
        
        return containers
    except Exception as e:
        logger.error(f"Error getting containers: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/history")
async def get_history(session_data: str = Depends(require_auth)):
    """Get update history"""
    try:
        return db.get_update_history(limit=50)
    except Exception as e:
        logger.error(f"Error getting history: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/versions/{container_name}")
async def get_versions(container_name: str, session_data: str = Depends(require_auth)):
    """Get available versions for a container"""
    try:
        versions = db.get_image_versions(container_name)
        
        # Since image_tag is now saved with the actual version number,
        # we just use it directly as the display tag
        for version in versions:
            version['display_tag'] = version['image_tag']
        
        return versions
    except Exception as e:
        logger.error(f"Error getting versions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/rollback-containers")
async def get_rollback_containers(session_data: str = Depends(require_auth)):
    """Get list of containers that have rollback versions available"""
    try:
        # Get all containers
        all_containers = monitor.client.containers.list()
        containers_with_versions = []
        
        for container in all_containers:
            versions = db.get_image_versions(container.name)
            if versions and len(versions) > 0:
                containers_with_versions.append({
                    "name": container.name,
                    "version_count": len(versions)
                })
        
        return containers_with_versions
    except Exception as e:
        logger.error(f"Error getting rollback containers: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/check-now")
async def check_now(session_data: str = Depends(require_auth)):
    """Trigger immediate update check"""
    try:
        # Get list of monitored containers to count them
        containers = monitor.get_monitored_containers()
        container_count = len(containers)
        
        # Add log entry for manual check
        db.add_check_log(
            container_name="All Containers",
            container_id="manual",
            current_image="",
            current_image_id="",
            message=f"Manually triggered update check for {container_count} containers"
        )
        
        # Run check in background
        import asyncio
        asyncio.create_task(monitor.check_all_containers())
        return {"success": True, "message": f"Checking {container_count} container{'s' if container_count != 1 else ''} for updates..."}
    except Exception as e:
        logger.error(f"Error triggering check: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/check-container")
async def check_container(data: Dict, session_data: str = Depends(require_auth)):
    """Check a specific container for updates"""
    try:
        container_name = data.get("container_name")
        check_only = data.get("check_only", False)
        
        if not container_name:
            raise HTTPException(status_code=400, detail="Missing container_name")
        
        if check_only:
            # Just check for updates, don't apply them (no email notifications)
            update_info = monitor.check_container_for_update(container_name, send_notifications=False)
            
            if update_info:
                return {
                    "update_available": True,
                    "current_image": monitor._get_image_version(update_info['old_image']),
                    "new_image": monitor._get_image_version(update_info['new_image'])
                }
            else:
                # Check if container exists and can be checked
                try:
                    current_image = monitor.get_container_image(container_name)
                    return {
                        "update_available": False,
                        "current_image": current_image
                    }
                except Exception as e:
                    return {
                        "error": True,
                        "message": f"Cannot check for updates: {str(e)}"
                    }
        else:
            # Run check and update in background
            import asyncio
            asyncio.create_task(monitor.check_single_container(container_name))
            
            return {"message": f"Checking {container_name} for updates..."}
    except Exception as e:
        logger.error(f"Error checking container: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/update-container")
async def update_container(data: Dict, session_data: str = Depends(require_auth)):
    """Update a specific container to latest version"""
    try:
        container_name = data.get("container_name")
        
        if not container_name:
            raise HTTPException(status_code=400, detail="Missing container_name")
        
        # Perform the update
        success = await monitor.update_single_container(container_name)
        
        if success:
            return {
                "success": True,
                "message": f"Successfully updated {container_name} to the latest version"
            }
        else:
            return {
                "success": False,
                "message": f"Failed to update {container_name}. Check logs for details."
            }
    except Exception as e:
        logger.error(f"Error updating container: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/rollback")
async def rollback(data: Dict, session_data: str = Depends(require_auth)):
    """Rollback a container to a previous version"""
    try:
        container_name = data.get("container_name")
        version_id = data.get("version_id")
        
        if not container_name or not version_id:
            raise HTTPException(status_code=400, detail="Missing parameters")
        
        # Get version info before rollback
        versions = db.get_image_versions(container_name)
        version = next((v for v in versions if v['id'] == version_id), None)
        
        if not version:
            return {"success": False, "message": "Version not found"}
        
        # Check if container is compose-managed
        try:
            container = monitor.client.containers.get(container_name)
            labels = container.labels
            is_compose = 'com.docker.compose.project' in labels
        except:
            is_compose = False
        
        result = await monitor.rollback_container(container_name, version_id)
        
        if result.get("success"):
            return {
                "success": True, 
                "message": f"Successfully rolled back {container_name}",
                "image_name": result.get("best_tag", version['image_name']),
                "is_compose": is_compose
            }
        else:
            error_msg = result.get("error", "Unknown error occurred")
            return {"success": False, "message": f"Failed to rollback {container_name}: {error_msg}"}
            
    except Exception as e:
        logger.error(f"Error during rollback: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/config")
async def get_config(session_data: str = Depends(require_auth)):
    """Get current configuration"""
    try:
        config_path = Path("config/config.yaml")
        if not config_path.exists():
            config_path = Path("config/config.example.yaml")
        
        with open(config_path, 'r') as f:
            config_data = yaml.safe_load(f)
        
        # Replace SMTP password with value from database (or masked placeholder)
        smtp_password = db.get_secure_setting("smtp_password")
        if smtp_password:
            # Show masked password in UI
            if config_data.get('notifications', {}).get('email'):
                config_data['notifications']['email']['password'] = '********'
        
        return config_data
    except Exception as e:
        logger.error(f"Error getting config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/config")
async def save_config(data: Dict, session_data: str = Depends(require_auth)):
    """Save configuration"""
    try:
        # Extract and save SMTP password to database if provided
        smtp_password = data.get('notifications', {}).get('email', {}).get('password', '')
        
        # Only update password if it's not the masked placeholder
        if smtp_password and smtp_password != '********':
            logger.info(f"Saving SMTP password to database (length: {len(smtp_password)})")
            db.set_secure_setting("smtp_password", smtp_password)
        else:
            logger.info(f"Skipping password save (empty or masked): '{smtp_password}'")
        
        # Remove password from config data before saving to file
        if data.get('notifications', {}).get('email'):
            data['notifications']['email']['password'] = ''
        
        config_path = Path("config/config.yaml")
        
        # Save to config.yaml
        with open(config_path, 'w') as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        
        return {
            "success": True, 
            "message": "Configuration saved successfully! Restart the container for changes to take effect."
        }
    except Exception as e:
        logger.error(f"Error saving config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/restart")
async def restart_container(session_data: str = Depends(require_auth)):
    """Restart the docker-updater container"""
    try:
        import docker
        import os
        
        client = docker.from_env()
        
        # Get the hostname which is the container ID
        hostname = os.uname().nodename
        
        # Find and restart the container
        container = client.containers.get(hostname)
        container.restart()
        
        return {"success": True, "message": "Container restarting..."}
    except Exception as e:
        logger.error(f"Error restarting container: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/test-email")
async def test_email(data: Dict, session_data: str = Depends(require_auth)):
    """Send a test email to verify SMTP settings"""
    try:
        from app.notifications import NotificationService
        
        # Extract email settings from request
        smtp_host = data.get("smtp_host")
        smtp_port = data.get("smtp_port", 587)
        use_tls = data.get("use_tls", True)
        username = data.get("username", "")
        password = data.get("password", "")
        from_address = data.get("from_address")
        to_addresses = data.get("to_addresses", [])
        
        # If password is empty, try to get it from database
        if not password:
            password = db.get_secure_setting("smtp_password") or ""
            logger.info(f"Retrieved password from database (length: {len(password) if password else 0})")
        else:
            logger.info(f"Using password from form (length: {len(password)})")
        
        if not smtp_host or not from_address or not to_addresses:
            raise HTTPException(status_code=400, detail="Missing required email settings")
        
        # Create a temporary notification service instance for testing
        notifier = NotificationService(config, db)
        result = notifier.send_test_email(
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            use_tls=use_tls,
            username=username,
            password=password,
            from_address=from_address,
            to_addresses=to_addresses
        )
        
        return result
        
    except Exception as e:
        logger.error(f"Error sending test email: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/toggle-monitoring/{container_name}")
async def toggle_monitoring(container_name: str, data: Dict, session_data: str = Depends(require_auth)):
    """Toggle monitoring for a specific container"""
    try:
        enabled = data.get("enabled", True)
        config_path = Path("config/config.yaml")
        
        # Read current config
        with open(config_path, 'r') as f:
            config_data = yaml.safe_load(f)
        
        # Get current exclude list
        exclude_list = config_data.get('monitoring', {}).get('exclude_containers', [])
        
        if enabled:
            # Enable monitoring - remove from exclude list
            if container_name in exclude_list:
                exclude_list.remove(container_name)
        else:
            # Disable monitoring - add to exclude list
            if container_name not in exclude_list:
                exclude_list.append(container_name)
        
        # Update config
        if 'monitoring' not in config_data:
            config_data['monitoring'] = {}
        config_data['monitoring']['exclude_containers'] = exclude_list
        
        # Save config
        with open(config_path, 'w') as f:
            yaml.dump(config_data, f, default_flow_style=False, sort_keys=False)
        
        # Reload config in memory
        from app.config import load_config
        global config
        config = load_config()
        monitor.config = config
        
        return {
            "success": True, 
            "message": f"Monitoring {'enabled' if enabled else 'disabled'} for {container_name}",
            "enabled": enabled
        }
        
    except Exception as e:
        logger.error(f"Error toggling monitoring for {container_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

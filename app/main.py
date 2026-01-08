import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import uvicorn

from app.config import load_config
from app.database import Database
from app.docker_monitor import DockerMonitor
from app.notifications import NotificationService
from app.web import routes

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

# Global instances
config = None
db = None
monitor = None
notifier = None
monitor_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    global config, db, monitor, notifier, monitor_task
    
    # Startup
    logger.info("Starting Whalekeeper...")
    
    # Load configuration
    config = load_config()
    logger.info(f"Loaded configuration (cron schedule: {config.cron_schedule})")
    
    # Initialize database
    db = Database()
    logger.info("Database initialized")
    
    # Initialize notification service
    notifier = NotificationService(config, db)
    logger.info("Notification service initialized")
    
    # Initialize Docker monitor
    monitor = DockerMonitor(config, db, notifier)
    logger.info("Docker monitor initialized")
    
    # Set global references for routes
    routes.monitor = monitor
    routes.db = db
    routes.config = config
    
    # Start monitoring in background
    monitor_task = asyncio.create_task(monitor.start_monitoring())
    logger.info("Monitoring task started")
    
    yield
    
    # Shutdown
    logger.info("Shutting down...")
    monitor.stop_monitoring()
    if monitor_task:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
    logger.info("Shutdown complete")


# Create FastAPI app
app = FastAPI(
    title="Whalekeeper",
    description="Automatic Docker image update monitoring and management",
    version="1.0.0",
    lifespan=lifespan
)

# Mount static files
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")

# Include routes
app.include_router(routes.router)


def main():
    """Run the application"""
    config = load_config()
    
    uvicorn.run(
        "app.main:app",
        host=config.web.host,
        port=config.web.port,
        log_level="info"
    )


if __name__ == "__main__":
    main()

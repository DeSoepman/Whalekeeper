import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import uvicorn

from app.config import load_config
from app.database import Database
from app.diagnostics import ErrorLogForwarder, LogBuffer, create_reporter
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
    
    # Initialize database
    db = Database()
    logger.info("Database initialized")

    # Load configuration
    config = load_config(db)
    logger.info(f"Loaded configuration (cron schedule: {config.cron_schedule})")
    
    # Initialize notification service
    notifier = NotificationService(config, db)
    logger.info("Notification service initialized")
    
    # Initialize Docker monitor
    monitor = DockerMonitor(config, db, notifier)
    logger.info("Docker monitor initialized")

    # Report failures to the log server (never let this stop the app from starting)
    log_buffer = LogBuffer()
    log_buffer.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logging.getLogger().addHandler(log_buffer)
    try:
        monitor.diagnostics = create_reporter(db, monitor.client, log_buffer)
        if monitor.diagnostics:
            forwarder = ErrorLogForwarder(monitor.diagnostics)
            # uvicorn's loggers don't propagate to the root logger.
            logging.getLogger().addHandler(forwarder)
            logging.getLogger("uvicorn").addHandler(forwarder)
            monitor.diagnostics.report("startup", "info", "Whalekeeper started")
            monitor.diagnostics.start_heartbeat()
    except Exception as e:
        logger.warning(f"Diagnostics reporting unavailable: {e}")

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
    if monitor.diagnostics:
        await monitor.diagnostics.close()
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
    config = load_config(Database())
    
    uvicorn.run(
        "app.main:app",
        host=config.web.host,
        port=config.web.port,
        log_level="info"
    )


if __name__ == "__main__":
    main()

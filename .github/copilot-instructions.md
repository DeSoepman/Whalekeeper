# GitHub Copilot Instructions for Whalekeeper

## Project Overview
Whalekeeper is a Docker container monitoring and auto-update system with rollback capabilities. It monitors containers for image updates, automatically updates them on schedule, and provides a web UI for management.

**Important**: This application is distributed via Docker registry. Keep the image as lightweight as possible - users will be pulling this image over the network. Avoid adding unnecessary dependencies or files that increase image size.

## Technology Stack
- **Backend**: Python 3.11, FastAPI, asyncio
- **Database**: SQLite with custom Database class
- **Docker**: Docker SDK for Python
- **Frontend**: Vanilla JavaScript, no framework
- **Authentication**: Bcrypt password hashing, session-based auth
- **Testing**: pytest with async support

## Code Style & Standards

### Python
- Use type hints for function parameters and return values
- Follow async/await patterns consistently
- Use descriptive variable names (no single letters except in loops)
- Prefer f-strings over .format() or % formatting
- Keep functions focused and under 50 lines when possible
- Use logging (logger.info, logger.error) instead of print statements

### Error Handling
- Always catch specific exceptions, avoid bare `except:`
- Log errors with context before re-raising
- Record failures in database with descriptive messages
- Send notifications on critical errors

### Database Operations
- Use the Database class methods, don't write raw SQL
- Always handle database exceptions gracefully
- Record update history for all container operations

## Project Structure

### Core Modules
- `app/main.py` - Application entry point, FastAPI setup
- `app/docker_monitor.py` - Core Docker monitoring logic
- `app/database.py` - SQLite database operations
- `app/config.py` - Configuration management
- `app/notifications.py` - Email/Discord/webhook notifications
- `app/web/routes.py` - Web API endpoints
- `app/web/templates/` - HTML templates
- `app/web/static/` - JavaScript and CSS

### Key Patterns

#### Docker Container Updates
1. Pull new image
2. Save current container config for rollback
3. Stop and remove old container
4. Create new container with same config
5. Monitor health checks
6. Rollback if health check fails

#### Async Operations
- All Docker operations should be async
- Use `await asyncio.sleep()` for delays
- Properly handle async context managers

#### Configuration
- Config file: `config/config.yaml`
- Sensitive data (SMTP passwords) encrypted in database
- Use the Config class for all config access

## Security Considerations
- Never log passwords or sensitive credentials
- Use read-only Docker socket mounts when possible
- Validate and sanitize all user inputs
- Use parameterized database queries (already handled by Database class)
- Encrypt sensitive data before storing in database

## Testing Guidelines
- Write tests in `tests/` directory
- Use pytest fixtures from `conftest.py`
- Mock Docker client in unit tests
- Mark tests as `@pytest.mark.unit` or `@pytest.mark.asyncio`
- Test both success and failure scenarios
- Keep tests focused and independent

## Common Patterns

### Adding a New Container Operation
```python
async def operation_name(self, container, **kwargs):
    try:
        logger.info(f"Starting operation for {container.name}")
        
        # Perform operation
        result = await self.some_async_operation()
        
        # Record in database
        self.db.add_update_history(
            container_name=container.name,
            status="success",
            message="Operation completed"
        )
        
        # Send notification
        if send_notification:
            await self.notifier.send_notification(...)
        
        return result
        
    except Exception as e:
        logger.error(f"Operation failed: {str(e)}")
        self.db.add_update_history(
            container_name=container.name,
            status="failed",
            message=f"Operation failed: {str(e)}"
        )
        raise
```

### Adding a New API Endpoint
```python
@router.post("/api/endpoint")
async def endpoint_name(request: Request, _: str = Depends(require_auth)):
    try:
        data = await request.json()
        # Validate input
        # Perform operation
        return {"success": True, "message": "Operation completed"}
    except Exception as e:
        logger.error(f"API error: {str(e)}")
        return {"success": False, "message": str(e)}
```

## Special Considerations

### Docker Compose Support
- Containers with `com.docker.compose.project` label are compose-managed
- Updating compose containers requires executing `docker compose up -d`
- Handle missing docker CLI gracefully (runs in container without docker binary)
- Don't install Docker CLI in the image to keep it lightweight - users can mount host's docker binary if needed

### Health Checks
- Use Docker's native health check if available
- Fall back to crash detection (container running check)
- Monitor for configurable duration after updates

### Rollback System
- Store previous image versions in database
- Keep last N versions (configurable)
- Rollback recreates container with old image
- Preserve container configuration across rollbacks

## Don't Do
- Don't add new dependencies without good reason (increases image size and download time for users)
- Don't install additional system packages in Dockerfile unless absolutely necessary
- Don't use global state (use dependency injection)
- Don't block the async event loop with sync operations
- Don't expose internal errors to users via API
- Don't modify user's docker-compose files
- Don't increase Docker image size unnecessarily - users pull this from registry
- Don't bundle large files or build artifacts in the Docker image

## Helpful Commands
- Run tests: `pytest tests/ -v`
- Run specific test: `pytest tests/test_file.py::test_name -v`
- Build image: `docker build -t whalekeeper:test .`
- Run locally: `python -m app.main`

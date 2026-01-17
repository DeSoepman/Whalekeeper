# Whalekeeper

Automatic Docker container updates with health checks and rollback support.

## What is this?

Whalekeeper monitors your Docker containers for image updates and automatically updates them on a schedule you define. If something goes wrong after an update, it automatically rolls back to the previous version.

It's basically a self-hosted alternative to watchtower with a web UI and better safety features.

## Features

**Updates:**
- Automatic update checks on a cron schedule
- Manual updates via web interface
- Exclude specific containers from updates
- Keeps previous image versions for rollback

**Health & Safety:**
- Monitors containers after updates using Docker health checks
- Automatically rolls back if container crashes or fails health checks
- No configuration needed - just works with your existing health checks
- Falls back to crash detection if no health check is defined

**Web Interface:**
- Dashboard showing all containers and their versions
- Update history with detailed logs
- One-click rollback to any previous version
- Configuration editor
- Built-in authentication

**Notifications:**
- Email (SMTP)
- Discord webhooks
- Custom webhooks
- Configurable for different events

## Quick Start

Create a `docker-compose.yml`:

```yaml
services:
  whalekeeper:
    image: desoepman/whalekeeper:latest
    container_name: whalekeeper
    restart: unless-stopped
    ports:
      - 5454:5454
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./config:/app/config
      - ./data:/app/data
    environment:
      - TZ=Europe/Brussels
```

Start it:

```bash
docker compose up -d
```

Open http://localhost:5454 and create your admin account.

That's it. Whalekeeper will start monitoring your containers based on the schedule you configure in the web UI.

## Installation

Pull from Docker Hub:
```bash
docker pull desoepman/whalekeeper:latest
```

Or build from source:
```bash
git clone https://github.com/desoepman/whalekeeper.git
cd whalekeeper
docker build -t whalekeeper:latest .
```

## Configuration

The config file (`config/config.yaml`) contains your settings. You can edit it through the web interface or manually.

Example config:

```yaml
# Update check schedule (cron format)
cron_schedule: "0 2 * * *"  # Daily at 2 AM

# Container monitoring
monitoring:
  exclude_containers:
    - whalekeeper
    - portainer

# Rollback settings
rollback:
  keep_versions: 3

# Web interface
web:
  port: 5454
```

The SMTP password is stored encrypted in the database for security. Other settings are in the config file.

### Cron schedule examples

```
*/30 * * * *   Every 30 minutes
0 */6 * * *    Every 6 hours
0 2 * * *      Daily at 2 AM
0 2 * * 1      Every Monday at 2 AM
0 0 1 * *      First day of each month
```

### Gmail setup

If you want email notifications with Gmail:

1. Enable 2FA on your Google account
2. Go to https://myaccount.google.com/apppasswords
3. Generate an app password
4. Use that password in Whalekeeper's email settings (via web UI)

The SMTP password is stored encrypted in the database, not in the config file.

## Usage

### Web interface

The dashboard shows all your containers with their current versions. From there you can:

- Manually trigger update checks
- Update individual containers
- Rollback to previous versions
- View update history
- Change settings
- Test your notification setup

### Health checks and auto-rollback

After updating a container, Whalekeeper monitors it to make sure it's actually working. No configuration needed.

How it works:
- If the container has a HEALTHCHECK defined, it waits for Docker to report it as healthy (up to 10 minutes)
- If no HEALTHCHECK exists, it just watches for crashes/restarts for 2 minutes

If something goes wrong (container crashes, health check fails, repeated restarts), Whalekeeper automatically rolls back to the previous version and sends you a notification.

For example, if you update nginx and it crashes because of a bad config, it'll be rolled back within seconds automatically.

### Manual rollback

On the Dashboard tab, use the Rollback section to select a container and a previous version, then click Rollback.

## Security

**Authentication:**
- First-run registration (create admin account on first launch)
- Bcrypt password hashing
- Session-based auth with HTTP-only cookies

**Credential storage:**
- SMTP password is encrypted in the database
- Other settings are in the config file

**For production use:**

Bind to localhost and use a reverse proxy:

```yaml
services:
  whalekeeper:
    image: desoepman/whalekeeper:latest
    container_name: whalekeeper
    restart: unless-stopped
    ports:
      - "127.0.0.1:5454:5454"  # Only localhost
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro  # Read-only
      - ./config:/app/config
      - ./data:/app/data
```

Then use nginx/Caddy/Traefik with SSL for external access.

## Contributing

Bug reports and pull requests are welcome on GitHub.

## License

MIT License - see LICENSE file for details.


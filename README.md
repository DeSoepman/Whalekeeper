# 🐳 Whalekeeper

<p align="center">
  <strong>Keep your Docker containers fresh and up-to-date, automatically.</strong>
</p>

<p align="center">
  <a href="#features">Features</a> •
  <a href="#quick-start">Quick Start</a> •
  <a href="#installation">Installation</a> •
  <a href="#configuration">Configuration</a> •
  <a href="#usage">Usage</a> •
  <a href="#security">Security</a>
</p>

---

## 📋 Overview

Whalekeeper is a self-hosted Docker container management tool that automatically monitors your containers for image updates, updates them with zero configuration hassle, and keeps you informed through multiple notification channels. With a clean web interface, rollback support, and enterprise-grade security, it's the set-it-and-forget-it solution for keeping your Docker infrastructure current.

## ✨ Features

### Core Functionality
- 🔄 **Automatic Updates** - Monitor containers on your schedule (cron-based)
- 🎯 **Smart Selection** - Monitor all containers or cherry-pick specific ones
- 🔙 **Rollback Support** - Instantly revert to previous image versions
- 📊 **Update History** - Track all updates with detailed logs
- 🏷️ **Version Detection** - Extracts and displays container versions from image labels

### Web Interface
- 🖥️ **Modern Dashboard** - Clean, responsive UI with real-time status
- 🔐 **Secure Authentication** - Session-based login with bcrypt password hashing
- ⚙️ **Live Configuration** - Update settings without restarting
- 📧 **Test Notifications** - Verify email/Discord settings instantly

### Notifications
- 📧 **Email (SMTP)** - Styled HTML emails with update details
- 💬 **Discord** - Webhook notifications with rich embeds
- 🔗 **Custom Webhooks** - Integrate with your own systems
- 🎨 **Customizable** - Control which events trigger notifications

### Security & Reliability
- 🔒 **Database-secured credentials** - SMTP passwords encrypted in SQLite
- 👤 **First-run registration** - Create admin account on first launch
- 🛡️ **Session security** - HTTP-only cookies with configurable expiry
- 📦 **Docker Compose detection** - Prevents conflicts with compose-managed containers

## 🚀 Quick Start

### Using Docker Compose (Recommended)

1. **Create docker-compose.yml:**

```yaml
services:
  whalekeeper:
    image: desoepman/whalekeeper:latest
    container_name: whalekeeper
    restart: unless-stopped
    ports:
      - "9090:8080"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock  # Required for Docker access
      - ./config:/app/config                        # Configuration
      - ./data:/app/data                            # Database
    environment:
      - TZ=Europe/Brussels  # Set your timezone
```

2. **Start the container:**

```bash
docker compose up -d
```

3. **Access the web interface:**

Open `http://localhost:9090` in your browser

4. **Complete first-run setup:**

- Create your admin account
- Configure notification settings
- Select containers to monitor

### Using Docker CLI

```bash
docker run -d \
  --name whalekeeper \
  -p 9090:8080 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v $(pwd)/config:/app/config \
  -v $(pwd)/data:/app/data \
  -e TZ=Europe/Brussels \
  --restart unless-stopped \
  desoepman/whalekeeper:latest
```

## 📦 Installation

### Option 1: Docker Hub

```bash
docker pull desoepman/whalekeeper:latest
```

### Option 2: GitHub Container Registry

```bash
docker pull ghcr.io/desoepman/whalekeeper:latest
```

### Option 3: Build from Source

```bash
git clone https://github.com/desoepman/whalekeeper.git
cd whalekeeper
docker build -t whalekeeper:latest .
```

## ⚙️ Configuration

Whalekeeper uses a simple YAML configuration file. Most settings can be managed through the web interface.

### config.yaml

```yaml
# Update check schedule (cron format)
cron_schedule: "0 2 * * *"  # Daily at 2 AM

# Container monitoring
monitoring:
  monitor_all: true           # Monitor all containers
  exclude_containers:         # Containers to skip
    - whalekeeper
    - portainer

# Notifications
notifications:
  email:
    enabled: true
    smtp_host: smtp.gmail.com
    smtp_port: 587
    use_tls: true
    username: your-email@gmail.com
    # Password stored securely in database
    from_address: your-email@gmail.com
    to_addresses:
      - recipient@example.com
    notify_on_update_found: true
    notify_on_success: true
    notify_on_error: true
  
  discord:
    enabled: false
    webhook_url: https://discord.com/api/webhooks/...
  
  webhook:
    enabled: false
    url: https://your-webhook-endpoint.com/notify

# Rollback settings
rollback:
  keep_versions: 3  # Number of previous versions to keep

# Web interface
web:
  port: 8080
```

### Cron Schedule Examples

```yaml
"*/30 * * * *"   # Every 30 minutes
"0 */6 * * *"    # Every 6 hours
"0 2 * * *"      # Daily at 2 AM
"0 2 * * 1"      # Every Monday at 2 AM
"0 0 1 * *"      # First day of each month
```

### Email Configuration (Gmail Example)

For Gmail, you'll need an App Password:

1. Enable 2-factor authentication on your Google account
2. Go to [App Passwords](https://myaccount.google.com/apppasswords)
3. Generate a password for "Mail"
4. Use this password in Whalekeeper's email settings

**Note:** SMTP passwords are stored encrypted in the database, not in the config file.

## 📖 Usage

### Web Interface

The dashboard provides:

- **Container Overview** - View all monitored containers with current versions
- **Manual Updates** - Trigger update checks or update individual containers
- **Rollback** - Revert containers to previous image versions
- **Logs** - View complete update history
- **Configuration** - Manage settings and monitoring preferences
- **Test Notifications** - Verify email/Discord configuration

### Container Selection

**Monitor All Containers:**
```yaml
monitoring:
  monitor_all: true
  exclude_containers:
    - whalekeeper  # Don't update yourself!
```

**Monitor Specific Containers:**
```yaml
monitoring:
  monitor_all: false
  exclude_containers: []  # Only monitored: containers with labels
```

Then add labels to containers you want monitored:
```yaml
labels:
  - "autoupdate=true"
```

### Manual Operations

**Check for updates now:**
- Click "Check Now" in the dashboard
- Or restart a specific container to pull latest image

**Rollback a container:**
- Navigate to Logs tab
- Click the menu icon (⋮) next to any update
- Select "Rollback to this version"

## 🔐 Security

Whalekeeper takes security seriously:

### Authentication
- **First-run registration** - Set up admin account on first launch
- **Bcrypt password hashing** - Industry-standard password security
- **Session-based auth** - Secure HTTP-only cookies
- **Auto-logout** - Configurable session expiry (30 days default)

### Credential Storage
- **Database encryption** - SMTP passwords stored encrypted in SQLite
- **No plaintext secrets** - Sensitive data never stored in config files
- **Environment isolation** - No environment variables for credentials

### Best Practices
- Always use strong passwords (minimum 8 characters)
- Keep Whalekeeper updated to the latest version
- Restrict network access to the web interface (use reverse proxy with SSL)
- Regularly review update logs for unexpected changes

### Production Deployment

For production use, consider:

```yaml
services:
  whalekeeper:
    image: desoepman/whalekeeper:latest
    container_name: whalekeeper
    restart: unless-stopped
    ports:
      - "127.0.0.1:9090:8080"  # Only allow local access
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro  # Read-only socket
      - ./config:/app/config
      - ./data:/app/data
    networks:
      - internal
```

Then use a reverse proxy (nginx, Caddy, Traefik) with SSL for external access.

## 🎨 Screenshots

<!-- Add screenshots here -->
<!-- Example: -->
<!-- ![Dashboard](docs/images/dashboard.png) -->
<!-- ![Logs](docs/images/logs.png) -->

## 🤝 Contributing

Contributions are welcome! Feel free to:

- Report bugs or request features via [Issues](https://github.com/desoepman/whalekeeper/issues)
- Submit Pull Requests
- Improve documentation
- Share your deployment experiences

## 📝 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 💬 Support

- **Issues**: [GitHub Issues](https://github.com/desoepman/whalekeeper/issues)
- **Discussions**: [GitHub Discussions](https://github.com/desoepman/whalekeeper/discussions)

## 🌟 Show Your Support

If you find Whalekeeper useful, give it a ⭐ on GitHub!

---

<p align="center">
  Made with 🐳 by the Whalekeeper team
</p>
    webhook_url: "https://discord.com/api/webhooks/YOUR_WEBHOOK_URL"
  
  webhook:
    enabled: true
    url: "https://your-webhook-endpoint.com/notify"
```

### 2. Run with Docker Compose

```bash
docker compose up -d
```

### 3. Access Web GUI

Open your browser to: `http://localhost:9090`

The web interface allows you to:
- View monitored containers
- See update history
- Trigger manual update checks
- Rollback containers to previous versions

## Configuration Options

### Monitoring

```yaml
monitoring:
  # Monitor all containers
  monitor_all: true
  
  # Or monitor only containers with specific labels
  monitor_all: false
  labels:
    - "autoupdate=true"
  
  # Exclude specific containers
  exclude_containers:
    - "docker-updater"
```

### Notifications

**Email (SMTP):**
```yaml
email:
  enabled: true
  smtp_host: "smtp.gmail.com"
  smtp_port: 587
  use_tls: true
  username: "your-email@gmail.com"
  password: "your-app-password"
  from_address: "your-email@gmail.com"
  to_addresses:
    - "recipient@example.com"
```

**Discord:**
```yaml
discord:
  enabled: true
  webhook_url: "https://discord.com/api/webhooks/..."
```

**Generic Webhook:**
```yaml
webhook:
  enabled: true
  url: "https://your-endpoint.com/notify"
  method: "POST"
  headers:
    Content-Type: "application/json"
```

### Rollback

```yaml
rollback:
  # Keep last 3 versions for rollback
  keep_versions: 3
```

## How It Works

1. **Cron Scheduling**: Based on the configured cron expression (e.g., `"0 22 * * 1"` for every Monday at 10 PM), the application checks all monitored containers
2. **Image Comparison**: For each container, it pulls the latest image and compares with the current one
3. **Update Process**: If an update is found:
   - Saves current configuration for rollback
   - Stops and removes old container
   - Creates new container with same config but new image
   - Sends notifications
4. **Rollback**: Previous versions are stored in SQLite database and can be restored via web GUI

## Usage Examples

### Monitor All Containers

Default behavior - monitors all running containers except those in `exclude_containers`.

### Monitor Specific Containers

Use Docker labels:

```bash
docker run -d --label autoupdate=true nginx:latest
```

Then in config:
```yaml
monitoring:
  monitor_all: false
  labels:
    - "autoupdate=true"
```

### Manual Update Check

Use the web GUI or API:

```bash
curl -X POST http://localhost:9090/api/check-now
```

### Rollback a Container

Use the web GUI to select the container and version, or use the API:

```bash
curl -X POST http://localhost:9090/api/rollback \
  -H "Content-Type: application/json" \
  -d '{"container_name": "my-container", "version_id": 1}'
```

## API Endpoints

- `GET /` - Web GUI
- `GET /api/containers` - List monitored containers
- `GET /api/history` - Get update history
- `GET /api/versions/{container_name}` - Get available versions for rollback
- `POST /api/check-now` - Trigger immediate update check
- `POST /api/rollback` - Rollback a container

## Security Notes

- The application requires access to Docker socket (`/var/run/docker.sock`)
- Consider using Docker socket proxy for production
- Use strong passwords for SMTP and web GUI authentication
- Run on internal network or behind reverse proxy with HTTPS

## Compatibility

### Minimum Requirements
- **Docker Engine:** 20.10+ (December 2020 or newer)
- **Linux Kernel:** 3.10+ (any modern distribution)
- **Architecture:** AMD64 (x86_64)

### Tested & Supported Linux Distributions
- Ubuntu 20.04, 22.04, 24.04+
- Debian 10, 11, 12+
- Proxmox VE 7.0+
- RHEL/CentOS 8+
- Fedora 35+
- Arch Linux
- Any distribution with Docker support

### Docker API Compatibility
- Compatible with Docker API 1.41 - 1.45+
- Automatically negotiates API version
- Future-proof with Python Docker SDK 7.0+

### Long-term Maintenance
- **OS Upgrades:** Safe to upgrade your Linux distribution - the app runs in an isolated container
- **Docker Upgrades:** Compatible with future Docker versions via auto-negotiation
- **Python Updates:** Base image uses Python 3.11 (supported until October 2027)
- **Rebuild Recommendation:** Rebuild every 6-12 months for security patches:
  ```bash
  docker compose build --no-cache && docker compose up -d
  ```

## Troubleshooting

**Container not updating:**
- Check if container is excluded in config
- Verify image name format (include tag, e.g., `nginx:latest`)
- Check logs: `docker compose logs -f`

**Notifications not working:**
- Verify SMTP credentials (use app-specific passwords for Gmail)
- Test Discord webhook URL in browser
- Check logs for error messages

**Rollback fails:**
- Ensure image is still available locally
- Check database for stored versions: `sqlite3 data/updater.db "SELECT * FROM image_versions;"`

## Development

Run locally without Docker:

```bash
# Install dependencies
pip install -r requirements.txt

# Run application
python -m app.main
```

## License

MIT License - feel free to use and modify!

## Contributing

Contributions welcome! Please open an issue or PR.

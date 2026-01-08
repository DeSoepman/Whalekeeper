FROM python:3.11-slim

# Labels for GitHub Container Registry
LABEL org.opencontainers.image.source="https://github.com/desoepman/whalekeeper"
LABEL org.opencontainers.image.description="Keep your Docker containers fresh and up-to-date, automatically"
LABEL org.opencontainers.image.licenses="MIT"

# Install Docker CLI
RUN apt-get update && \
    apt-get install -y docker.io && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY config/ ./config/
COPY VERSION ./VERSION

# Create data directory
RUN mkdir -p /app/data

# Expose web interface port
EXPOSE 9090

# Run the application
CMD ["python", "-m", "app.main"]

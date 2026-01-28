FROM python:3.11-slim

# Accept version as build argument
ARG VERSION=dev

# Labels for GitHub Container Registry
LABEL org.opencontainers.image.source="https://github.com/desoepman/whalekeeper"
LABEL org.opencontainers.image.description="Keep your Docker containers fresh and up-to-date, automatically"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.version="${VERSION}"

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY config/ ./config/
COPY VERSION ./VERSION
COPY entrypoint.sh /entrypoint.sh

# Make entrypoint executable
RUN chmod +x /entrypoint.sh

# Create data directory
RUN mkdir -p /app/data

# Expose web interface port
EXPOSE 5454

# Use entrypoint to handle config initialization
ENTRYPOINT ["/entrypoint.sh"]

# Run the application
CMD ["python", "-m", "app.main"]

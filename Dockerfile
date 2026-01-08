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

# Create data directory
RUN mkdir -p /app/data

# Expose web interface port
EXPOSE 5454

# Run the application
CMD ["python", "-m", "app.main"]

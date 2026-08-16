# ==============================================================================
# Dockerfile for QuotaManager Gateway
# Debian Bookworm based with kernel networking tools (nftables, iproute2, dnsmasq)
# ==============================================================================

FROM python:3.12-slim-bookworm AS base

# Prevent Python from writing .pyc files, enable unbuffered output and set PYTHONPATH
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DEBIAN_FRONTEND=noninteractive

# Install runtime system packages needed for networking, traffic shaping & dns
RUN apt-get update && apt-get install -y --no-install-recommends \
    nftables \
    iproute2 \
    dnsmasq \
    procps \
    arp-scan \
    kmod \
    curl \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements-linux.txt /app/requirements-linux.txt
RUN pip install --no-cache-dir -r requirements-linux.txt

# Create runtime directories
RUN mkdir -p /var/lib/quota-gateway \
             /var/log/quota-gateway \
             /etc/dnsmasq.d \
             /var/lib/misc \
             /app/data

# Copy systemctl compatibility shim and entrypoint
COPY scripts/docker-systemctl-shim.sh /usr/local/bin/systemctl
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/systemctl /usr/local/bin/docker-entrypoint.sh

# Copy application source files
COPY api/ /app/api/
COPY core/ /app/core/
COPY quota/ /app/quota/
COPY web/ /app/web/
COPY scripts/ /app/scripts/
COPY run.py /app/run.py
COPY config.yaml /app/config.default.yaml

# Expose Web Dashboard Port (default 8080) and DNS / DHCP ports
EXPOSE 8080 53/udp 53/tcp 67/udp

# Set volume mount points
VOLUME ["/var/lib/quota-gateway", "/var/log/quota-gateway", "/etc/dnsmasq.d"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python3", "run.py", "--config", "/app/config.yaml"]

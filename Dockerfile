# syntax=docker/dockerfile:1

# ── Build / dependency stage ──────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Install dependencies into a separate prefix so we can copy them cleanly
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="gude-exporter" \
      org.opencontainers.image.description="Prometheus exporter for Gude Expert Power Control (EPC) PDUs" \
      org.opencontainers.image.source="https://github.com/avxugent/gude-exporter"

WORKDIR /app

# Pick up Debian security fixes newer than the base image, and drop pip: the
# runtime never installs anything, and pip's vendored libraries would otherwise
# show up as separate, unfixable-by-us findings in image scans.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /usr/local/lib/python3*/site-packages/pip \
              /usr/local/lib/python3*/site-packages/pip-*.dist-info \
              /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.*

# Copy installed packages from the builder stage
COPY --from=builder /install /usr/local

# Copy the application source
COPY gude_exporter/ ./gude_exporter/

# Bundle the example config as the built-in default.
# A custom config can be injected at runtime by mounting a file over this path
# or by setting GUDE_CONFIG to point elsewhere.
RUN mkdir -p /etc/gude-exporter
COPY config.example.yaml /etc/gude-exporter/config.yaml

ENV GUDE_CONFIG=/etc/gude-exporter/config.yaml

EXPOSE 9880

# Run as a non-root user for security
RUN useradd --no-create-home --shell /bin/false exporter
USER exporter

ENTRYPOINT ["python", "-m", "gude_exporter"]

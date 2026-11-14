# Build from the repository root:  docker build -f services/notification/Dockerfile .
FROM python:3.12-slim

# Pick up Debian security fixes published after the base image was built.
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     PIP_DISABLE_PIP_VERSION_CHECK=1     SWIFTFLOW_CONTRACTS_DIR=/app/contracts/schemas

WORKDIR /app
COPY requirements-dev.lock.txt /build/constraints.txt
COPY libs/common /build/common
COPY services/notification/requirements.txt /build/requirements.txt
RUN pip install --no-cache-dir -c /build/constraints.txt -r /build/requirements.txt /build/common     && python -m pip uninstall -y pip && rm -rf /build     && useradd --system --uid 10001 --no-create-home app

COPY libs/contracts/schemas /app/contracts/schemas
COPY services/notification/app /app/app
COPY services/notification/migrations /app/migrations

USER 10001
EXPOSE 8003
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003", "--no-server-header"]

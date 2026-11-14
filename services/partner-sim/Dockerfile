# Build from the repository root:  docker build -f services/partner-sim/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     PIP_DISABLE_PIP_VERSION_CHECK=1     SWIFTFLOW_CONTRACTS_DIR=/app/contracts/schemas

WORKDIR /app
COPY requirements-dev.lock.txt /build/constraints.txt
COPY libs/common /build/common
COPY services/partner-sim/requirements.txt /build/requirements.txt
RUN pip install --no-cache-dir -c /build/constraints.txt -r /build/requirements.txt /build/common     && rm -rf /build     && useradd --system --uid 10001 --no-create-home app

COPY libs/contracts/schemas /app/contracts/schemas
COPY services/partner-sim/app /app/app


USER 10001
EXPOSE 8004
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8004", "--no-server-header"]

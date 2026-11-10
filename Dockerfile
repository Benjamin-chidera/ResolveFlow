FROM python:3.12-slim

# Install system dependencies and uv binary from official image
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Enable bytecode compilation and unbuffered logging
ENV UV_COMPILE_BYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"

# Copy dependency specifications
COPY requirements.txt pyproject.toml uv.lock ./

# Install project dependencies with uv
RUN uv sync --frozen --no-install-project

# Copy project source code
COPY . .

# Final project sync
RUN uv sync --frozen

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]


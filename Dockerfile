FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN pip install --no-cache-dir uv \
    && uv sync --locked --no-dev --extra dashboard
ENV PATH="/app/.venv/bin:$PATH"
CMD ["uv", "run", "--no-sync", "uvicorn", "gtts.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

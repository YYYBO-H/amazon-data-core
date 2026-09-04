FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE .gitignore .dockerignore ./
COPY src/amazon_data_core/__init__.py ./src/amazon_data_core/__init__.py
RUN pip install --no-cache-dir -e '.[dev,agent]'
COPY src ./src
COPY examples ./examples
COPY scripts ./scripts
COPY tests ./tests

EXPOSE 8080
CMD ["uvicorn", "amazon_data_core.api:app", "--host", "0.0.0.0", "--port", "8080"]

FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src
RUN pip install --no-cache-dir .

CMD ["uvicorn", "x_digest.main:app", "--host", "0.0.0.0", "--port", "8000"]

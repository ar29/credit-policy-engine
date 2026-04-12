#!/bin/bash

# 1. Wait for Postgres to be ready
echo "Waiting for postgres..."
while ! nc -z postgres 5432; do
  sleep 0.1
done
echo "PostgreSQL started"

# 2. Run the migration script
echo "Running database migrations..."
python -c "from app.models.schemas import Base; from sqlalchemy import create_engine; engine = create_engine('postgresql://temporal:temporal@postgres:5432/temporal'); Base.metadata.create_all(engine)"

# 3. Start the application
echo "Starting FastAPI..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
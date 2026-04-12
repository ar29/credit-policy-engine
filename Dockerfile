# Use a slim version of Python 3.10
FROM python:3.10-slim

# Set environment variables for Python
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/code

# Set working directory
WORKDIR /code

# Install system dependencies required for psycopg2 and Temporal
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    curl \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Give execution rights to the prestart script
RUN chmod +x prestart.sh

# Expose the FastAPI port
EXPOSE 8000

CMD ["./prestart.sh"]
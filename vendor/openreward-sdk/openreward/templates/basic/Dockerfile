FROM python:3.11-slim

RUN apt update && apt upgrade -y && apt install -y \
    curl

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY server.py .

# Expose port
EXPOSE 8000

# Start the server
CMD ["python", "server.py"]

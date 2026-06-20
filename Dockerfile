FROM python:3.11-slim

# Installiamo p7zip-full per gestire zip, rar, 7z e curl/wget per download
RUN apt-get update && apt-get install -y \
    p7zip-full \
    curl \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

CMD ["python", "-u", "main.py"]


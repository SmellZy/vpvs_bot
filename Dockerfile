FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py admin_panel.py start.py ./
COPY templates/ ./templates/

# За замовчуванням запускаємо бота.
# Для адмін-панелі встанови CMD або використовуй Procfile.
CMD ["python", "start.py"]

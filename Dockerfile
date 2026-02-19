FROM python:3.11-slim

# System deps for Playwright Chromium
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxrandr2 libgbm1 libasound2 \
    libpango-1.0-0 libpangocairo-1.0-0 \
    libxshmfence1 libx11-xcb1 \
    fonts-liberation libappindicator3-1 \
    --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser (Chromium only)
RUN playwright install chromium
RUN playwright install-deps chromium

COPY . .

# Create download directory
RUN mkdir -p /app/downloads

CMD ["python", "bot.py"]

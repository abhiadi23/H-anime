FROM python:3.11-slim-bookworm

WORKDIR /app
COPY . /app/

# install system deps (gcc for TgCrypto + ffmpeg for rename/convert)
RUN apt-get update && apt-get install -y \
    gcc \
    libffi-dev \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip
RUN pip install -r requirements.txt

CMD ["python", "bot.py"]

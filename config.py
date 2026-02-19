import os

# Telegram Bot Credentials
API_ID = int(os.environ.get("API_ID", "27322718"))
API_HASH = os.environ.get("API_HASH", "4f6d1b67cf101aea5cf0536885aa1b82")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8564241159:AAGBkeIvZyzlrqyGY8_bTivOCWDKHgmLwgM")

# Download Settings
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "./downloads")
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "2000"))  # 2GB default

# Bot Settings
MAX_SEARCH_RESULTS = int(os.environ.get("MAX_SEARCH_RESULTS", "10"))
HEADLESS_BROWSER = os.environ.get("HEADLESS_BROWSER", "true").lower() == "true"

# Proxy (optional)
PROXY_URL = os.environ.get("PROXY_URL", "")  # e.g. http://user:pass@host:port

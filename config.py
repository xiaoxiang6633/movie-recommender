import os

SECRET_KEY = os.environ.get("SECRET_KEY", os.urandom(24).hex())
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"

PPXZY_USERNAME = os.environ.get("PPXZY_USERNAME", "")
PPXZY_PASSWORD = os.environ.get("PPXZY_PASSWORD", "")

CRAWL_DELAY = 1.5
CRAWL_TIMEOUT = 15
MAX_MOVIES = 20
FETCH_RESOURCE_LINKS = True

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

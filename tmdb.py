import logging
import time

import requests

from config import HEADERS, TMDB_API_KEY as _CONFIG_TMDB_KEY, TMDB_IMAGE_BASE, CRAWL_TIMEOUT

logger = logging.getLogger(__name__)

TMDB_API_KEY = _CONFIG_TMDB_KEY
_cache: dict[str, str] = {}


def _get_key() -> str:
    return TMDB_API_KEY


def fetch_poster(imdb_id: str) -> str:
    """Get movie poster URL from TMDB by IMDb ID. Returns empty string on failure."""
    key = _get_key()
    if not imdb_id or not key:
        return ""

    if imdb_id in _cache:
        return _cache[imdb_id]

    url = "https://api.themoviedb.org/3/find/" + imdb_id
    params = {"api_key": key, "external_source": "imdb_id"}

    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=CRAWL_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("TMDB lookup failed for %s: %s", imdb_id, e)
        _cache[imdb_id] = ""
        return ""

    results = data.get("movie_results", [])
    if not results:
        _cache[imdb_id] = ""
        return ""

    poster_path = results[0].get("poster_path")
    if not poster_path:
        _cache[imdb_id] = ""
        return ""

    poster_url = TMDB_IMAGE_BASE + poster_path
    _cache[imdb_id] = poster_url
    time.sleep(0.1)
    return poster_url


def enrich_movies(movies: list[dict]) -> list[dict]:
    """Enrich movie list with TMDB posters (replaces low-quality ppxzy images)."""
    if not _get_key():
        logger.info("TMDB_API_KEY not set, skipping poster enrichment")
        return movies

    updated = 0
    for m in movies:
        imdb = m.get("imdb", "")
        if imdb and (not m.get("poster") or "360buyimg" in m.get("poster", "")):
            poster = fetch_poster(imdb)
            if poster:
                m["poster"] = poster
                updated += 1

    logger.info("TMDB enriched %d posters", updated)
    return movies

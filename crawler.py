import re
import time
import logging
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from config import HEADERS, CRAWL_DELAY, CRAWL_TIMEOUT, MAX_MOVIES

logger = logging.getLogger(__name__)

PPXZY_CATEGORIES = {
    # top-level
    "dy": 83,
    "dh": 1,
    "jj": 84,
    "jlp": 223,
    # subcategories
    "hydy": 264,   # 华语电影
    "omdy": 262,   # 欧美电影
    "rhdy": 263,   # 日韩电影
    "qtdy": 265,   # 其他电影
    "hyjj": 260,   # 华语剧集
    "omjj": 258,   # 欧美剧集
    "rhjj": 259,   # 日韩剧集
    "qtjj": 261,   # 其他剧集
}

PPXZY_GENRES = {
    "juqing": 123,    # 剧情
    "xiju": 114,      # 喜剧
    "jingsong": 118,  # 惊悚
    "aiq": 126,       # 爱情
    "xuanyi": 117,    # 悬疑
    "fanzui": 115,    # 犯罪
    "kehuan": 121,    # 科幻
    "kongbu": 134,    # 恐怖
    "dongzuo": 120,   # 动作
    "qihuan": 116,    # 奇幻
    "donghua": 144,   # 动画
    "jilupian": 150,  # 纪录片
    "tongxing": 122,  # 同性
    "lishi": 129,     # 历史
    "zhuanji": 136,   # 传记
    "zhanzheng": 130, # 战争
    "maoxian": 135,   # 冒险
    "jiating": 124,   # 家庭
    "yinyue": 125,    # 音乐
    "gewu": 132,      # 歌舞
    "xibu": 147,      # 西部
    "guzhuang": 133,  # 古装
    "wuxia": 143,     # 武侠
    "zainan": 148,    # 灾难
    "ertong": 146,    # 儿童
}


def crawl(url: str, category: str = "", genre: str = "") -> list[dict]:
    """Crawl a movie listing page and return extracted movie data."""
    domain = urlparse(url).netloc.lower()

    if "douban.com" in domain:
        return _crawl_douban(url)

    if "ppxzy.top" in domain:
        return _crawl_ppxzy(url, category=category, genre=genre)

    return _crawl_generic(url)


# ─── ppxzy.top (WordPress REST API) ─────────────────────────────

def _ppxzy_api(path: str, params: dict | None = None) -> dict:
    url = f"https://ppxzy.top/wp-json/wp/v2/{path}"
    resp = requests.get(url, params=params, headers=HEADERS, timeout=CRAWL_TIMEOUT)
    resp.raise_for_status()
    return resp


def _crawl_ppxzy(url: str, category: str = "", genre: str = "") -> list[dict]:
    if category:
        cat_id = PPXZY_CATEGORIES.get(category, 83)
    else:
        parsed = urlparse(url)
        path = parsed.path.strip("/")
        cat_id = PPXZY_CATEGORIES.get(path, 83)

    tag_id = PPXZY_GENRES.get(genre) if genre else None

    movies = []
    page = 1
    per_page = 50
    tag_names = {}

    while len(movies) < MAX_MOVIES:
        params = {"categories": cat_id, "per_page": per_page, "page": page, "_embed": 1}
        if tag_id:
            params["tags"] = str(tag_id)
        logger.info("PPXZY API page %d (category=%d, tag=%s)", page, cat_id, tag_id or "none")

        try:
            resp = _ppxzy_api("posts", params)
        except Exception as e:
            logger.error("Failed to fetch page %d: %s", page, e)
            break

        posts = resp.json()
        if not posts:
            break

        for post in posts:
            movie = _parse_ppxzy_post(post, tag_names)
            if movie and movie["title"]:
                movies.append(movie)
                if len(movies) >= MAX_MOVIES:
                    break

        total_pages = int(resp.headers.get("X-WP-TotalPages", "1"))
        if page >= total_pages or len(movies) >= MAX_MOVIES:
            break

        page += 1
        time.sleep(CRAWL_DELAY)

    logger.info("PPXZY crawled %d items from category %d", len(movies), cat_id)
    return movies


def _parse_ppxzy_post(post: dict, tag_names: dict) -> dict | None:
    title_html = post.get("title", {}).get("rendered", "")
    title_text = re.sub(r"<[^>]+>", "", title_html).strip()

    parsed = _parse_ppxzy_title(title_text)

    content_html = post.get("content", {}).get("rendered", "")
    content_text = re.sub(r"<[^>]+>", "", content_html)
    content_text = re.sub(r"\s+", " ", content_text).strip()

    director = ""
    actors = ""
    imdb = ""
    description = ""

    director_m = re.search(r"导演[:\s]*([^\n.]+?)(?:主演|编剧|类型|制片|语言|上映|又名|IMDb|$)", content_text)
    if director_m:
        director = director_m.group(1).strip().rstrip("/").strip()

    actors_m = re.search(r"主演[:\s]*([^\n.]+?)(?:类型|制片|语言|上映|又名|IMDb|$)", content_text)
    if actors_m:
        actors = actors_m.group(1).strip().rstrip("/").strip()

    imdb_m = re.search(r"IMDb[:\s]*(tt\d+)", content_text)
    if imdb_m:
        imdb = imdb_m.group(1)

    plot_m = re.search(r"剧情简介[^·]*[·]+\s*(.+?)(?:$)", content_text)
    if plot_m:
        description = plot_m.group(1).strip()[:300]
    if not description:
        description = content_text[:300]
    description = re.sub(r"^[·\s]+", "", description)

    poster = ""
    soup = BeautifulSoup(content_html, "lxml")
    img_el = soup.select_one("img")
    if img_el:
        poster = img_el.get("src", "")

    tags = []
    if "_embedded" in post and "wp:term" in post["_embedded"]:
        for group in post["_embedded"]["wp:term"]:
            for term in group:
                name = term.get("name", "")
                if name:
                    tags.append(name)

    return {
        "title": parsed["title"],
        "original_title": parsed.get("original_title", ""),
        "year": parsed.get("year", ""),
        "director": director,
        "genre": parsed.get("genre", ", ".join(tags) if tags else ""),
        "rating": parsed.get("rating", "暂无评分"),
        "description": description,
        "poster": poster,
        "imdb": imdb,
        "url": post.get("link", ""),
    }


def _parse_ppxzy_title(title: str) -> dict:
    result = {"title": title, "original_title": "", "year": "", "genre": "", "rating": "暂无评分"}

    brackets = re.findall(r"\[([^\]]+)\]", title)
    if len(brackets) < 2:
        return result

    result["title"] = brackets[0].strip()

    for segment in brackets[1:]:
        segment = segment.strip()
        if re.match(r"^\d{4}$", segment):
            result["year"] = segment
        elif re.match(r"^\d\.\d{1,2}$", segment):
            result["rating"] = segment
        elif re.match(r"^[A-Za-z\s.&!?,:;'\-()]+$", segment) and len(segment) > 2:
            result["original_title"] = segment
        else:
            if not result["genre"]:
                result["genre"] = segment
            else:
                result["genre"] += f" / {segment}"

    return result


# ─── Douban ─────────────────────────────────────────────────────

def _fetch(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=CRAWL_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return BeautifulSoup(resp.text, "lxml")


def _crawl_douban(start_url: str) -> list[dict]:
    movies = []
    base = "https://movie.douban.com/top250"
    page_size = 25

    parsed = urlparse(start_url)
    qs_start = re.search(r"start=(\d+)", parsed.query)
    start = int(qs_start.group(1)) if qs_start else 0

    while len(movies) < MAX_MOVIES:
        url = f"{base}?start={start}&filter="
        logger.info("Crawling page: %s", url)

        try:
            soup = _fetch(url)
        except Exception as e:
            logger.error("Failed to fetch %s: %s", url, e)
            break

        items = soup.select("div.item")
        if not items:
            items = soup.select("div[class*=item]") or soup.select("li[class*=item]")

        for item in items:
            movie = _parse_douban_item(item)
            if movie and movie["title"]:
                movies.append(movie)
                if len(movies) >= MAX_MOVIES:
                    break

        has_next = soup.select_one("span.next a") or soup.select_one("a[rel=next]")
        if not has_next or len(movies) >= MAX_MOVIES:
            break

        start += page_size
        time.sleep(CRAWL_DELAY)

    logger.info("Crawled %d movies from Douban", len(movies))
    return movies


def _parse_douban_item(item) -> dict | None:
    title_el = item.select_one("span.title")
    if not title_el:
        return None

    title = title_el.text.strip()

    original_el = item.select_one("span.title:nth-of-type(2)")
    original_title = original_el.text.strip().lstrip("/").strip() if original_el else ""

    link_el = item.select_one("div.hd a") or item.select_one("a")
    link = link_el.get("href", "") if link_el else ""

    rating_el = item.select_one("span.rating_num")
    rating = rating_el.text.strip() if rating_el else "暂无评分"

    quote_el = item.select_one("span.inq")
    quote = quote_el.text.strip() if quote_el else ""

    info_el = item.select_one("div.bd p") or item.select_one("p")
    director = year = genre = ""
    if info_el:
        info_text = info_el.get_text(" ", strip=True)
        info_text = re.sub(r"\s+", " ", info_text)
        director_match = re.search(r"导演[:\s]*([^一-鿿]*?)(?:主演|\d{4}|$)", info_text)
        if director_match:
            director = director_match.group(1).strip().rstrip("/").strip()
        year_match = re.search(r"(\d{4})", info_text)
        if year_match:
            year = year_match.group(1)
        parts = info_text.split("/")
        if len(parts) >= 3:
            genre = parts[-1].strip()

    poster_el = item.select_one("img")
    poster = poster_el.get("src", "") if poster_el else ""

    return {
        "title": title,
        "original_title": original_title,
        "year": year,
        "director": director,
        "genre": genre,
        "rating": rating,
        "description": quote,
        "poster": poster,
        "url": link,
        "imdb": "",
    }


# ─── Generic fallback ───────────────────────────────────────────

def _crawl_generic(url: str) -> list[dict]:
    movies = []
    try:
        soup = _fetch(url)
    except Exception as e:
        logger.error("Failed to fetch %s: %s", url, e)
        return movies

    candidates = (
        soup.select("div.movie-item")
        or soup.select("div.movie")
        or soup.select("div.card")
        or soup.select("li.movie")
        or soup.select("article")
        or soup.select("div.item")
    )

    if not candidates:
        candidates = soup.select("a[href]")

    seen = set()
    for el in candidates[:MAX_MOVIES]:
        link_el = el if el.name == "a" else el.select_one("a[href]")
        link = link_el.get("href", "") if link_el else ""
        if link and not link.startswith("http"):
            link = urljoin(url, link)

        text = el.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text)
        if len(text) < 6 or text in seen:
            continue
        seen.add(text)

        rating_match = re.search(r"(\d\.\d{1,2})", text)
        rating = rating_match.group(1) if rating_match else "暂无评分"

        year_match = re.search(r"(\d{4})", text)
        year = year_match.group(1) if year_match else ""

        title = text.split(" ")[0][:50] if text else "未知"

        img_el = el.select_one("img") if el.name != "img" else el
        poster = img_el.get("src", "") if img_el else ""

        movies.append({
            "title": title,
            "original_title": "",
            "year": year,
            "director": "",
            "genre": "",
            "rating": rating,
            "description": text[:200] if len(text) > 50 else "",
            "poster": poster,
            "url": link,
            "imdb": "",
        })

    logger.info("Generic crawl found %d items from %s", len(movies), url)
    return movies

import re
import time
import logging
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from config import (
    HEADERS, CRAWL_DELAY, CRAWL_TIMEOUT, MAX_MOVIES,
    PPXZY_USERNAME, PPXZY_PASSWORD, FETCH_RESOURCE_LINKS,
)

logger = logging.getLogger(__name__)

# Module-level state exposed to app.py so the frontend can report login status.
# Reset before each crawl; _ppxzy_login sets them.
_ppxzy_login_failed = False
_ppxzy_login_attempted = False
_ppxzy_login_error = ""

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

# Known cloud storage domains and their labels
CLOUD_DOMAINS = {
    "pan.baidu.com": "百度网盘",
    "pan.baidu": "百度网盘",
    "aliyundrive.com": "阿里云盘",
    "alipan.com": "阿里云盘",
    "pan.quark.cn": "夸克网盘",
    "xunlei.com": "迅雷云盘",
    "pan.xunlei.com": "迅雷云盘",
    "115.com": "115网盘",
    "ctfile.com": "城通网盘",
    "lanzou": "蓝奏云",
    "feijipan.com": "飞机盘",
    "caiyun.139.com": "中国移动云盘",
    "yunpan": "360云盘",
    "uc.cn": "UC网盘",
}

_session: requests.Session | None = None


def _ppxzy_login(username: str = "", password: str = "") -> requests.Session | None:
    """Login to ppxzy.top and return an authenticated session, or None on failure."""
    global _session, _ppxzy_login_failed, _ppxzy_login_attempted, _ppxzy_login_error

    user = username or PPXZY_USERNAME
    pwd = password or PPXZY_PASSWORD

    if _session is not None:
        return _session

    session = requests.Session() # 开一个"浏览器标签页"
    session.headers.update(HEADERS)

    if not user or not pwd:
        logger.info("No ppxzy credentials provided, skipping login")
        _session = session
        return _session

    _ppxzy_login_attempted = True
    _ppxzy_login_failed = False
    _ppxzy_login_error = ""

    login_url = "https://ppxzy.top/user/login"
    admin_ajax = "https://ppxzy.top/wp-admin/admin-ajax.php"

    login_ok = False
    try:
        # Step 1: Fetch login page to get session cookies
        # 第一步：GET 登录页，服务器返回 Set-Cookie: PHPSESSID=abc123
        resp = session.get(login_url, timeout=CRAWL_TIMEOUT)
        resp.raise_for_status()
        logger.info("ppxzy login: got initial cookies")

        # Step 2: Submit login via AJAX endpoint (no nonce required)
        login_data = {
            "action": "xintheme_login",
            "login_name": user,
            "password": pwd,
        }

        headers = dict(HEADERS)
        headers.update({
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": login_url,
            "Origin": "https://ppxzy.top",
        })
        #登录
        # 第二步：POST 账号密码，服务器返回 Set-Cookie: wordpress_logged_in=xxx
        resp = session.post(admin_ajax, data=login_data, headers=headers, timeout=CRAWL_TIMEOUT)
        logger.info("ppxzy login response: %s", resp.text[:200])

        try:
            result = resp.json()
            if result.get("state") == 200:
                logger.info("ppxzy login successful")
                login_ok = True
            else:
                _ppxzy_login_error = result.get("tips", "账号或密码错误")
                logger.warning("ppxzy login failed: %s", _ppxzy_login_error)
        except Exception:
            _ppxzy_login_error = "服务器返回异常"
            logger.warning("ppxzy login response not JSON: %s", resp.text[:100])

    except Exception as e:
        _ppxzy_login_error = f"登录请求失败: {e}"
        logger.error("ppxzy login error: %s", e)

    if login_ok:
        _session = session
        return _session
    else:
        _ppxzy_login_failed = True
        logger.warning("ppxzy login unsuccessful, resource links will not be fetched")
        _session = None
        return None


def _fetch_resource_links(post_url: str, session: requests.Session | None = None) -> list[dict]:
    """Fetch a ppxzy post page and extract cloud drive resource links.

    ppxzy uses the "download-info-page" plugin. The download URLs are loaded
    dynamically via AJAX (action=wb_dlipp_front). We reverse-engineer that flow:
    1. Fetch the post page to get wb_dlipp_config (pid) and available data-rid values
    2. Call the AJAX endpoint for each rid to get the real download URL + password
    """
    if session is None:
        session = _ppxzy_login()

    logger.info("─── [fetch-resources] %s ───", post_url[-40:])
    resources = []
    try:
        resp = session.get(post_url, timeout=CRAWL_TIMEOUT)
        logger.info("  [step1] GET post page → HTTP %d", resp.status_code)

        if resp.status_code != 200:
            logger.warning("  [FAIL] HTTP status %d, aborting", resp.status_code)
            return resources

        if "wp-login" in resp.url:
            logger.warning("  [FAIL] redirected to wp-login — session cookies not valid")
            return resources

        html = resp.text
        logger.info("  [step1] page size: %d bytes, final URL: %s", len(html), resp.url[:80])

        soup = BeautifulSoup(html, "lxml")

        # ── Extract pid ──
        pid = ""
        config_m = re.search(r"wb_dlipp_config\s*=\s*\{([^}]+)\}", html)
        if config_m:
            config_text = config_m.group(1)
            pid_m = re.search(r"pid\s*:\s*(\d+)", config_text)
            if pid_m:
                pid = pid_m.group(1)
                logger.info("  [step2] wb_dlipp_config found, pid=%s", pid)
            else:
                logger.warning("  [step2] wb_dlipp_config found but pid not in: %s", config_text[:120])
        else:
            logger.warning("  [step2] wb_dlipp_config NOT found in page HTML (plugin inactive or renamed?)")

        # Fallback: extract pid from post URL
        if not pid:
            pid_m = re.search(r"/(\d+)\.html", post_url)
            if pid_m:
                pid = pid_m.group(1)
                logger.info("  [step2] pid fallback from URL: %s", pid)
            else:
                logger.warning("  [step2] cannot determine pid from URL either: %s", post_url)

        # ── Find download buttons ──
        dl_buttons = soup.select(".dlipp-dl-btn, .j-wbdlbtn-dlipp, a[data-rid]")

        # Fallback: broader selectors (site may have updated CSS)
        if not dl_buttons:
            dl_buttons = soup.select("[data-rid]")
        if not dl_buttons:
            dl_buttons = soup.select(".dl-btn, .download-btn, [class*=dl], [class*=download]")

        logger.info("  [step3] CSS selectors matched %d download-button candidate(s)", len(dl_buttons))

        rid_labels = {}
        for btn in dl_buttons:
            rid = btn.get("data-rid", "").strip()
            if rid:
                label = btn.get_text(" ", strip=True) or rid
                rid_labels[rid] = label

        # Regex fallback: find data-rid directly in HTML
        if not rid_labels:
            rid_matches = re.findall(r'data-rid\s*=\s*["\']([^"\']+)["\']', html)
            for rid in rid_matches:
                rid_labels[rid] = rid
            if rid_matches:
                logger.info("  [step3] regex fallback found %d data-rid(s) in raw HTML: %s",
                           len(rid_matches), list(rid_labels.keys()))

        if not rid_labels:
            logger.warning("  [step3] ZERO download buttons — no data-rid found by CSS or regex")
            # Dump a snippet of HTML around likely download areas
            snippet = html[html.find("dlipp"):html.find("dlipp")+400] if "dlipp" in html else ""
            if snippet:
                logger.warning("  [step3] HTML snippet around 'dlipp': %s", snippet[:300])
            else:
                # Try around content area
                content_start = max(0, html.find("entry-content") - 50)
                if content_start >= 0:
                    logger.warning("  [step3] HTML near entry-content: ...%s...", html[content_start:content_start+400])
            return resources

        # ── Check for paywall ──
        vk_el = soup.select_one(".wb-vk-wp, .vk-paid-content, [class*='vk-pay']")
        if vk_el:
            logger.info("  [step4] VK paywall detected (may block AJAX)")

        # ── Call AJAX endpoint for each rid ──
        admin_ajax = "https://ppxzy.top/wp-admin/admin-ajax.php"
        ajax_headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": post_url,
            "Origin": "https://ppxzy.top",
        }
        #拿到 pid 和所有 rid 后，对每个网盘发一次 POST：
        for rid, label in rid_labels.items():
            data = {"action": "wb_dlipp_front", "pid": pid, "rid": rid}#获取链接
            try:
                r = session.post(admin_ajax, data=data, headers=ajax_headers, timeout=CRAWL_TIMEOUT)
                logger.info("  [step5] AJAX rid=%s → HTTP %d, body preview: %s",
                           rid, r.status_code, r.text[:150])

                if r.status_code != 200 or not r.text.strip():
                    logger.warning("  [step5] AJAX rid=%s empty or bad status, skipping", rid)
                    continue

                try:
                    result = r.json()
                except Exception:
                    logger.warning("  [step5] AJAX rid=%s response is not JSON: %s", rid, r.text[:200])
                    continue

                code = result.get("code")
                logger.info("  [step5] AJAX rid=%s code=%s", rid, code)

                if code == 0:
                    dl_data = result.get("data", {})
                    dl_url = dl_data.get("url", "")
                    dl_pwd = dl_data.get("pwd", "")

                    matched_type = label
                    for domain, cname in CLOUD_DOMAINS.items():
                        if dl_url and domain in dl_url.lower():
                            matched_type = cname
                            break
                    #每解析一个网盘就追加
                    resources.append({
                        "type": matched_type,
                        "url": dl_url,
                        "code": dl_pwd,
                        "label": label if label != matched_type else matched_type,
                    })
                    logger.info("  [step5] SUCCESS rid=%s → %s (pwd=%s)", rid, dl_url[:80], dl_pwd or "(none)")

                elif code == 2:
                    logger.warning("  [step5] rid=%s → code=2 '请先登录'", rid)
                elif code == 3:
                    logger.info("  [step5] rid=%s → code=3 '请评论'", rid)
                else:
                    logger.warning("  [step5] rid=%s → unexpected code=%s, response: %s",
                                   rid, code, r.text[:200])

            except Exception as e:
                logger.warning("  [step5] AJAX rid=%s exception: %s", rid, e)

    except Exception as e:
        logger.warning("  [FAIL] outer exception: %s", e)

    logger.info("─── [fetch-resources] done, got %d resource(s) ───", len(resources))
    return resources

# 入口函数：根据域名分发，返回 list[dict]
def crawl(url: str, category: str = "", genre: str = "",
          ppxzy_user: str = "", ppxzy_pass: str = "") -> list[dict]:
    """Crawl a movie listing page and return extracted movie data."""
    domain = urlparse(url).netloc.lower()
    # 爬豆瓣
    if "douban.com" in domain:
        return _crawl_douban(url)
    # 爬皮皮虾
    if "ppxzy.top" in domain:
        return _crawl_ppxzy(url, category=category, genre=genre,
                            username=ppxzy_user, password=ppxzy_pass)

    return _crawl_generic(url)


# ─── ppxzy.top (WordPress REST API) ─────────────────────────────

def _ppxzy_api(path: str, params: dict | None = None) -> dict:
    url = f"https://ppxzy.top/wp-json/wp/v2/{path}"# 
    resp = requests.get(url, params=params, headers=HEADERS, timeout=CRAWL_TIMEOUT)
    resp.raise_for_status()
    return resp


def _crawl_ppxzy(url: str, category: str = "", genre: str = "",
                 username: str = "", password: str = "") -> list[dict]:
    global _ppxzy_login_failed, _ppxzy_login_attempted, _ppxzy_login_error

    # Reset login state for this crawl
    _ppxzy_login_failed = False
    _ppxzy_login_attempted = False
    _ppxzy_login_error = ""

    if category:
        cat_id = PPXZY_CATEGORIES.get(category, 83)
    else:
        parsed = urlparse(url)
        path = parsed.path.strip("/")
        cat_id = PPXZY_CATEGORIES.get(path, 83)

    tag_id = PPXZY_GENRES.get(genre) if genre else None

    want_resources = FETCH_RESOURCE_LINKS and (username or password or PPXZY_USERNAME)
    session = None
    if want_resources:
        logger.info("Attempting ppxzy login for resource links...")
        session = _ppxzy_login(username, password)
        if session is None:
            logger.warning("ppxzy login failed — will crawl movie metadata without resource links")
            want_resources = False

    movies = []
    page = 1
    per_page = 50
    tag_names = {}
    #
    while len(movies) < MAX_MOVIES:
        params = {"categories": cat_id, "per_page": per_page, "page": page, "_embed": 1}
        if tag_id:
            params["tags"] = str(tag_id)
        logger.info("PPXZY API page %d (category=%d, tag=%s)", page, cat_id, tag_id or "none")

        try:
            resp = _ppxzy_api("posts", params)# ← 就这一行，真正发出 HTTP 请求
        except Exception as e:
            logger.error("Failed to fetch page %d: %s", page, e)
            break

        posts = resp.json()
        if not posts:
            break
        #每个电影对应的文章
        for post in posts:
            movie = _parse_ppxzy_post(post, tag_names)
            if movie and movie["title"]:
                # Fetch resource links for this movie
                if want_resources and session and movie.get("url"):
                    post_url = movie["url"]
                    logger.info("Fetching resource links from %s", post_url)
                    resources = _fetch_resource_links(post_url, session)# ← 这一步拿的是这一部电影的网盘链接
                    movie["resources"] = resources
                    if resources:
                        logger.info("  Found %d resource links for %s", len(resources), movie["title"])
                    time.sleep(CRAWL_DELAY * 0.5)

                movies.append(movie)
                if len(movies) >= MAX_MOVIES:
                    break
        #总页数
        total_pages = int(resp.headers.get("X-WP-TotalPages", "1"))
        if page >= total_pages or len(movies) >= MAX_MOVIES:
            break

        page += 1
        time.sleep(CRAWL_DELAY)

    logger.info("PPXZY crawled %d items from category %d", len(movies), cat_id)
    return movies

# 正文提取
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

    # Strip resource links section that trails after the actual plot summary.
    # ppxzy posts often append download info after the description.
    stop_at = re.search(r"(?:百度|阿里|夸克|迅雷|115|城通|蓝奏|飞机|移动|360|UC)(?:网盘|云盘)|"
                        r"(?:资源)?下载(?:链接|地址)?[：:]|"
                        r"提取码[：:]|"
                        r"https?://pan\.|"
                        r"https?://www\.aliyundrive|"
                        r"https?://pan\.quark", description)
    if stop_at:
        description = description[:stop_at.start()].strip()

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
        "resources": [],
    }

#标题解析
def _parse_ppxzy_title(title: str) -> dict:
    result = {"title": title, "original_title": "", "year": "", "genre": "", "rating": "暂无评分"}
    # brackets = ["流浪地球", "The Wandering Earth", "2019", "8.3", "科幻 / 冒险"]
    brackets = re.findall(r"\[([^\]]+)\]", title)
    if len(brackets) < 2:
        return result

    result["title"] = brackets[0].strip()#第一个元素是中文片名

    for segment in brackets[1:]:
        segment = segment.strip()
        if re.match(r"^\d{4}$", segment):#纯四位数字（年份）
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
        "resources": [],
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
            "resources": [],
        })

    logger.info("Generic crawl found %d items from %s", len(movies), url)
    return movies

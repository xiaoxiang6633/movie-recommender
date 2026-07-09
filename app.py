import json
import logging
import os

from flask import Flask, jsonify, render_template, request

from config import SECRET_KEY
from crawler import crawl
from recommender import recommend
from tmdb import enrich_movies

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

app = Flask(__name__)
app.secret_key = SECRET_KEY
_movie_cache: list[dict] = []


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/crawl", methods=["POST"])
def api_crawl():
    data = request.get_json()
    url = (data or {}).get("url", "").strip()
    category = (data or {}).get("category", "")
    genre = (data or {}).get("genre", "")
    tmdb_key = (data or {}).get("tmdb_key", "")
    ppxzy_user = (data or {}).get("ppxzy_user", "")
    ppxzy_pass = (data or {}).get("ppxzy_pass", "")
    if not url:
        return jsonify({"ok": False, "error": "请输入网址"}), 400

    app.logger.info("Crawling: %s (category=%s, genre=%s)", url, category, genre)
    if tmdb_key:
        import tmdb
        tmdb.TMDB_API_KEY = tmdb_key
    try:
        movies = crawl(url, category=category, genre=genre,
                       ppxzy_user=ppxzy_user, ppxzy_pass=ppxzy_pass)
        movies = enrich_movies(movies)
    except Exception as e:
        app.logger.error("Crawl error: %s", e)
        return jsonify({"ok": False, "error": f"爬取失败：{e}"}), 500

    # Count movies that have resource links
    movies_with_resources = sum(1 for m in movies if m.get("resources") and len(m["resources"]) > 0)
    total_resources = sum(len(m.get("resources", [])) for m in movies)

    # Read ppxzy login state (set by crawler module)
    import crawler
    login_ok = not crawler._ppxzy_login_failed if crawler._ppxzy_login_attempted else None
    login_error = crawler._ppxzy_login_error

    global _movie_cache
    _movie_cache = movies
    return jsonify({
        "ok": True,
        "count": len(movies),
        "movies": movies,
        "resources_found": total_resources,
        "movies_with_resources": movies_with_resources,
        "ppxzy_login": {
            "attempted": crawler._ppxzy_login_attempted,
            "ok": login_ok,
            "error": login_error,
        },
    })


@app.route("/api/recommend", methods=["POST"])
def api_recommend():
    data = request.get_json()
    user_query = (data or {}).get("query", "").strip()
    api_key = (data or {}).get("api_key", "").strip()

    movies = _movie_cache
    if not movies:
        return jsonify({"ok": False, "error": "请先爬取电影数据"}), 400
    if not user_query:
        return jsonify({"ok": False, "error": "请输入你的观影偏好"}), 400
    if not api_key:
        return jsonify({"ok": False, "error": "请提供 DeepSeek API Key"}), 400

    app.logger.info("Getting recommendations for: %s", user_query)
    try:
        result = recommend(movies, user_query, api_key)
        # Enrich recommendations with resource links from cache
        for rec in result.get("recommendations", []):
            rec_title = rec.get("title", "")
            rec_year = rec.get("year", "")
            for m in movies:
                if m.get("title") == rec_title and str(m.get("year", "")) == str(rec_year):
                    rec["resources"] = m.get("resources", [])
                    break
    except Exception as e:
        app.logger.error("Recommend error: %s", e)
        return jsonify({"ok": False, "error": f"推荐失败：{e}"}), 500

    return jsonify({"ok": True, "data": result})


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    prod = os.environ.get("PRODUCTION", "")

    if prod:
        from waitress import serve
        serve(app, host=host, port=port)
    else:
        app.run(host=host, port=port, debug=True)

import json
import logging

from openai import OpenAI

from config import DEEPSEEK_BASE_URL

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个电影推荐专家。用户会提供一份可观看的电影列表（JSON格式）和他们的观影偏好。

请根据用户的偏好，从电影列表中选出最匹配的 2-5 部电影推荐给用户。

要求：
1. 只推荐列表中实际存在的电影
2. 每部推荐包含：电影名、年份、评分，以及推荐理由（结合用户偏好说明为什么推荐）
3. 推荐理由要具体，不要泛泛而谈
4. 如果没有特别匹配的，诚实告知并推荐评分最高的几部

返回格式（严格JSON）：
{
  "recommendations": [
    {
      "title": "电影名",
      "year": "年份",
      "rating": "评分",
      "reason": "推荐理由"
    }
  ],
  "summary": "整体推荐总结"
}"""


def recommend(movies: list[dict], user_query: str, api_key: str) -> dict:
    """Call DeepSeek API to get personalized movie recommendations."""
    if not movies:
        return {"recommendations": [], "summary": "没有可用的电影数据，请先爬取电影信息。"}

    if not api_key:
        return {
            "recommendations": [],
            "summary": "请提供 DeepSeek API Key（可在 https://platform.deepseek.com 获取）。",
        }
    #Deepseek   创建 OpenAI 客户端时用掉api_key
    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
    #精简电影数据，只上传给Deepseek6个字段
    minimal_movies = []
    for m in movies:
        entry = {
            "title": m.get("title", ""),
            "year": m.get("year", ""),
            "rating": m.get("rating", ""),
            "genre": m.get("genre", ""),
            "director": m.get("director", ""),
            "description": m.get("description", ""),
        }
        minimal_movies.append(entry)

    catalog = json.dumps(minimal_movies, ensure_ascii=False, indent=2)

    user_message = (
        f"以下是可观看的电影列表（共 {len(minimal_movies)} 部）：\n\n"
        f"```json\n{catalog}\n```\n\n"
        f"用户的观影偏好：{user_query}\n\n"
        f"请根据用户偏好推荐最合适的电影。"
    )

    try:
        #DeepSeek 服务器校验 sk-xxx，扣费，返回推荐结果
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0.7,
            max_tokens=2048,
        )

        raw = resp.choices[0].message.content.strip()
        logger.info("DeepSeek raw response length: %d", len(raw))

        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()

        result = json.loads(raw)
        return result

    except json.JSONDecodeError:
        logger.warning("Failed to parse DeepSeek JSON response")
        return {
            "recommendations": [],
            "summary": f"API 返回格式解析失败，原始响应：{raw[:500]}",
        }
    except Exception as e:
        logger.error("DeepSeek API error: %s", e)
        error_msg = str(e)
        if "401" in error_msg or "unauthorized" in error_msg.lower():
            return {"recommendations": [], "summary": "API Key 无效，请检查后重试。"}
        if "429" in error_msg:
            return {"recommendations": [], "summary": "API 请求过于频繁，请稍后重试。"}
        if "insufficient" in error_msg.lower():
            return {"recommendations": [], "summary": "API 余额不足，请充值后重试。"}
        return {"recommendations": [], "summary": f"API 调用失败：{error_msg}"}

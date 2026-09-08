from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CAPABILITY_ROOT = Path.home() / "Desktop" / "小红书能力包搭建"
ENV_PATH = CAPABILITY_ROOT / "mcp" / ".env"
PROMPT_FRAMEWORK_PATH = CAPABILITY_ROOT / "小红书配图提示词框架.txt"

TIKHUB_BASE_URL = "https://api.tikhub.io"
XHS_SEARCH_ENDPOINT = "/api/v1/xiaohongshu/app_v2/search_notes"
XHS_DETAIL_ENDPOINT = "/api/v1/xiaohongshu/app_v2/get_image_note_detail"
ARK_IMAGE_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
DEFAULT_SEEDREAM_MODEL = "doubao-seedream-5.0-lite"
SEEDREAM_MODEL_ALIASES = {
    # Volcengine model IDs are often published with a dated hyphenated suffix.
    # Keep the user-facing name as the default while allowing easy fallback.
    "doubao-seedream-5.0-lite": "doubao-seedream-5-0-lite-260128",
    "Doubao-Seedream-5.0-lite": "doubao-seedream-5-0-lite-260128",
    "doubao-seedream-5-0-lite": "doubao-seedream-5-0-lite-260128",
    "doubao-seedream-5-0-lite-260128": "doubao-seedream-5-0-lite-260128",
}

ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')
HASHTAG_RE = re.compile(r"#([\w\u4e00-\u9fff-]+)")


class XHSMCPError(RuntimeError):
    pass


@dataclass(slots=True)
class NoteSummary:
    note_id: str
    title: str
    score: int
    like_count: int
    collect_count: int
    comment_count: int
    share_count: int
    raw: dict[str, Any]


@dataclass(slots=True)
class NoteContent:
    note_id: str
    title: str
    body: str
    topics: list[str]
    score: int
    stats: dict[str, int]
    raw: dict[str, Any]


def load_dotenv(path: Path = ENV_PATH) -> dict[str, str]:
    if not path.exists():
        raise XHSMCPError(f"未找到 .env 文件: {path}")
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        env[key.strip()] = value
    return env


def get_required_env(name: str) -> str:
    value = load_dotenv().get(name)
    if not value:
        raise XHSMCPError(f".env 中缺少或未配置 {name}")
    return value


def safe_filename(value: str, fallback: str = "xhs", max_length: int = 80) -> str:
    value = ILLEGAL_FILENAME_CHARS.sub("_", value).strip(" ._")
    value = re.sub(r"\s+", "_", value)
    return (value[:max_length].strip(" ._") or fallback)


def as_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    multipliers = {
        "万": 10000,
        "w": 10000,
        "W": 10000,
        "k": 1000,
        "K": 1000,
    }
    for suffix, multiplier in multipliers.items():
        if text.endswith(suffix):
            try:
                return int(float(text[:-1]) * multiplier)
            except ValueError:
                return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def walk(value: Any):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def first_string(data: Any, keys: tuple[str, ...]) -> str:
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for nested in data.values():
            found = first_string(nested, keys)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = first_string(item, keys)
            if found:
                return found
    return ""


def first_dict_value(data: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(data, dict):
        for key in keys:
            if key in data:
                return data[key]
        for nested in data.values():
            found = first_dict_value(nested, keys)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(data, list):
        for item in data:
            found = first_dict_value(item, keys)
            if found not in (None, "", [], {}):
                return found
    return None


def candidate_note_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in walk(payload):
        if not isinstance(item, dict):
            continue
        note_id = first_string(item, ("note_id", "id", "noteId"))
        if not note_id:
            continue
        has_note_signal = any(
            key in item
            for key in (
                "note_card",
                "display_title",
                "interact_info",
                "liked_count",
                "collected_count",
                "comment_count",
            )
        )
        if has_note_signal and id(item) not in seen:
            seen.add(id(item))
            candidates.append(item)
    return candidates


def extract_note_id(item: dict[str, Any]) -> str:
    return first_string(item, ("note_id", "noteId", "id"))


def extract_title(item: dict[str, Any]) -> str:
    return first_string(
        item,
        (
            "title",
            "display_title",
            "desc_title",
            "note_title",
            "name",
        ),
    )


def extract_body(item: dict[str, Any]) -> str:
    return first_string(
        item,
        (
            "desc",
            "description",
            "content",
            "text",
            "note_desc",
            "note_content",
        ),
    )


def count_from_keys(item: dict[str, Any], keys: tuple[str, ...]) -> int:
    value = first_dict_value(item, keys)
    return as_int(value)


def extract_stats(item: dict[str, Any]) -> dict[str, int]:
    like_count = count_from_keys(item, ("liked_count", "like_count", "likes", "likedCount"))
    collect_count = count_from_keys(
        item,
        ("collected_count", "collect_count", "fav_count", "favorite_count", "collectedCount"),
    )
    comment_count = count_from_keys(
        item,
        ("comment_count", "comments_count", "commentCount"),
    )
    share_count = count_from_keys(item, ("share_count", "shareCount"))
    return {
        "like_count": like_count,
        "collect_count": collect_count,
        "comment_count": comment_count,
        "share_count": share_count,
    }


def extract_topics(item: dict[str, Any], title: str = "", body: str = "") -> list[str]:
    topics: list[str] = []
    for value in walk(item):
        if isinstance(value, dict):
            for key in ("name", "tag_name", "topic_name", "title"):
                text = value.get(key)
                if isinstance(text, str) and text.strip().startswith("#"):
                    topics.append(text.strip().lstrip("#"))
            if any(k in value for k in ("tag_id", "topic_id", "hashtag_id")):
                text = first_string(value, ("name", "tag_name", "topic_name", "title"))
                if text:
                    topics.append(text.strip().lstrip("#"))
        elif isinstance(value, str):
            topics.extend(match.strip() for match in HASHTAG_RE.findall(value))

    topics.extend(match.strip() for match in HASHTAG_RE.findall(f"{title}\n{body}"))
    deduped: list[str] = []
    for topic in topics:
        topic = topic.strip(" #")
        if topic and topic not in deduped:
            deduped.append(topic)
    return deduped


def parse_summary(item: dict[str, Any]) -> NoteSummary:
    note_id = extract_note_id(item)
    stats = extract_stats(item)
    score = (
        stats["like_count"]
        + stats["collect_count"]
        + stats["comment_count"]
        + stats["share_count"]
    )
    return NoteSummary(
        note_id=note_id,
        title=extract_title(item),
        score=score,
        like_count=stats["like_count"],
        collect_count=stats["collect_count"],
        comment_count=stats["comment_count"],
        share_count=stats["share_count"],
        raw=item,
    )


def parse_detail(note_id: str, detail: dict[str, Any], fallback: NoteSummary) -> NoteContent:
    title = extract_title(detail) or fallback.title or note_id
    body = extract_body(detail)
    topics = extract_topics(detail, title, body)
    stats = extract_stats(detail)
    if not any(stats.values()):
        stats = {
            "like_count": fallback.like_count,
            "collect_count": fallback.collect_count,
            "comment_count": fallback.comment_count,
            "share_count": fallback.share_count,
        }
    score = sum(stats.values()) or fallback.score
    return NoteContent(
        note_id=note_id,
        title=title,
        body=body,
        topics=topics,
        score=score,
        stats=stats,
        raw=detail,
    )


def format_markdown(keyword: str, notes: list[NoteContent]) -> str:
    lines = [
        f"# 小红书关键词对标内容收集：{keyword}",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "- 排序口径：图文笔记，按点赞 + 收藏 + 评论 + 分享综合分选取前 3 篇",
        "",
    ]
    for index, note in enumerate(notes, start=1):
        topics = "、".join(f"#{topic}" for topic in note.topics) or "无"
        lines.extend(
            [
                f"## {index}. {note.title}",
                "",
                f"- 笔记 ID：{note.note_id}",
                f"- 综合分：{note.score}",
                f"- 点赞：{note.stats['like_count']}",
                f"- 收藏：{note.stats['collect_count']}",
                f"- 评论：{note.stats['comment_count']}",
                f"- 分享：{note.stats['share_count']}",
                f"- 话题：{topics}",
                "",
                "### 正文",
                "",
                note.body or "未提取到正文",
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


class XHSClient:
    def __init__(self, timeout: float = 30.0):
        self.tikhub_key = get_required_env("TIKHUB_KEY")
        self.doubao_key = get_required_env("DOUBAO_API_KEY")
        self.timeout = timeout

    def tikhub_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tikhub_key}"}

    def doubao_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.doubao_key}",
            "Content-Type": "application/json",
        }

    async def search_notes(
        self,
        keyword: str,
        page: int = 1,
        sort_type: str = "popularity_descending",
        note_type: str = "普通笔记",
        time_filter: str = "不限",
    ) -> dict[str, Any]:
        params = {
            "keyword": keyword,
            "page": page,
            "sort_type": sort_type,
            "note_type": note_type,
            "time_filter": time_filter,
            "source": "explore_feed",
            "ai_mode": 0,
        }
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(
                f"{TIKHUB_BASE_URL}{XHS_SEARCH_ENDPOINT}",
                params=params,
                headers=self.tikhub_headers(),
            )
            response.raise_for_status()
            return response.json()

    async def fetch_detail(self, note_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(
                f"{TIKHUB_BASE_URL}{XHS_DETAIL_ENDPOINT}",
                params={"note_id": note_id},
                headers=self.tikhub_headers(),
            )
            response.raise_for_status()
            return response.json()

    async def collect_top_notes(
        self,
        keyword: str,
        top_k: int = 3,
        page: int = 1,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        search_payload = await self.search_notes(keyword=keyword, page=page)
        summaries = [parse_summary(item) for item in candidate_note_items(search_payload)]
        summaries = [summary for summary in summaries if summary.note_id]
        deduped: dict[str, NoteSummary] = {}
        for summary in summaries:
            if summary.note_id not in deduped or summary.score > deduped[summary.note_id].score:
                deduped[summary.note_id] = summary
        top_notes = sorted(deduped.values(), key=lambda note: note.score, reverse=True)[:top_k]
        if not top_notes:
            raise XHSMCPError(f"没有从搜索结果中提取到笔记列表: {keyword}")

        details: list[NoteContent] = []
        for summary in top_notes:
            detail_payload = await self.fetch_detail(summary.note_id)
            details.append(parse_detail(summary.note_id, detail_payload, summary))

        if output_path:
            path = Path(output_path).expanduser()
            if not path.is_absolute():
                path = PROJECT_ROOT / path
        else:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = PROJECT_ROOT / f"xhs_benchmark_{safe_filename(keyword)}_{stamp}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(format_markdown(keyword, details), encoding="utf-8")

        latest = PROJECT_ROOT / "xhs_benchmark_latest.md"
        latest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

        return {
            "keyword": keyword,
            "output_path": str(path.resolve()),
            "latest_path": str(latest.resolve()),
            "notes": [
                {
                    "note_id": note.note_id,
                    "title": note.title,
                    "body": note.body,
                    "topics": note.topics,
                    "score": note.score,
                    "stats": note.stats,
                }
                for note in details
            ],
        }

    async def generate_image(self, prompt: str, model: str, size: str) -> bytes:
        body = {
            "model": SEEDREAM_MODEL_ALIASES.get(model, model),
            "prompt": prompt,
            "response_format": "b64_json",
            "size": size,
            "watermark": False,
        }
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            response = None
            for attempt in range(4):
                response = await client.post(
                    ARK_IMAGE_ENDPOINT,
                    headers=self.doubao_headers(),
                    json=body,
                )
                if response.status_code not in {429, 500, 502, 503, 504}:
                    break
                if attempt < 3:
                    await asyncio.sleep(20 * (attempt + 1))
            assert response is not None
            if response.is_error:
                raise XHSMCPError(f"生图接口失败 {response.status_code}: {response.text[:1000]}")
            payload = response.json()

            item = None
            data = payload.get("data")
            if isinstance(data, list) and data:
                item = data[0]
            elif isinstance(data, dict):
                item = data
            if not isinstance(item, dict):
                raise XHSMCPError(f"未识别的生图响应结构: {payload}")

            b64_value = item.get("b64_json") or item.get("image_base64") or item.get("base64")
            if isinstance(b64_value, str) and b64_value:
                if "," in b64_value and b64_value.startswith("data:image"):
                    b64_value = b64_value.split(",", 1)[1]
                return base64.b64decode(b64_value)

            url = item.get("url") or item.get("image_url")
            if isinstance(url, str) and url:
                image_response = await client.get(url)
                image_response.raise_for_status()
                return image_response.content

        raise XHSMCPError(f"响应中没有可保存的图片数据: {payload}")

    async def generate_xhs_images(
        self,
        markdown_path: str | None = None,
        output_dir: str | None = None,
        model: str = DEFAULT_SEEDREAM_MODEL,
        size: str = "1664x2240",
    ) -> dict[str, Any]:
        source_path = find_markdown_source(markdown_path)
        copy_text = source_path.read_text(encoding="utf-8")
        prompts = build_image_prompts(copy_text)

        root = Path(output_dir).expanduser() if output_dir else PROJECT_ROOT
        if not root.is_absolute():
            root = PROJECT_ROOT / root
        root.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        files: list[dict[str, str]] = []
        for index, prompt in enumerate(prompts, start=1):
            image = await self.generate_image(prompt=prompt, model=model, size=size)
            path = root / f"xhs_seedream_{stamp}_{index}.png"
            path.write_bytes(image)
            files.append({"path": str(path.resolve()), "prompt": prompt})

        return {
            "source_markdown": str(source_path.resolve()),
            "model": model,
            "size": size,
            "files": files,
        }


def find_markdown_source(markdown_path: str | None) -> Path:
    if markdown_path:
        path = Path(markdown_path).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.exists():
            raise XHSMCPError(f"未找到文案 Markdown: {path}")
        return path

    latest = PROJECT_ROOT / "xhs_benchmark_latest.md"
    if latest.exists():
        return latest

    candidates = sorted(PROJECT_ROOT.glob("xhs_benchmark_*.md"), key=lambda p: p.stat().st_mtime)
    if candidates:
        return candidates[-1]
    raise XHSMCPError("项目根目录未找到 xhs_benchmark_*.md，请先运行关键词对标内容收集工具")


def markdown_heading(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line.lstrip("# ").strip()
    return "小红书图文内容"


def summarize_copy(text: str, max_length: int = 420) -> str:
    clean = re.sub(r"```.*?```", " ", text, flags=re.S)
    clean = re.sub(r"[*_`>#-]+", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:max_length]


def framework_summary() -> str:
    if PROMPT_FRAMEWORK_PATH.exists():
        text = PROMPT_FRAMEWORK_PATH.read_text(encoding="utf-8")
        fixed = text.split("## 二、变量部分", 1)[0]
    else:
        fixed = (
            "可爱手绘涂鸦，线条柔和圆润，有轻微手工感；米白或浅奶油底色，"
            "马卡龙柔色，竖版 3:4 构图，主体明确，多留白。"
        )
    return re.sub(r"\s+", " ", fixed).strip()


def build_image_prompts(copy_text: str) -> list[str]:
    title = markdown_heading(copy_text)
    summary = summarize_copy(copy_text)
    topics = "、".join(sorted(set(HASHTAG_RE.findall(copy_text)))) or "围绕文案核心主题"
    style = framework_summary()
    variants = [
        (
            "封面主视觉",
            "提炼文案最强吸引点，设计一个清晰、有记忆点的封面画面，主体居中，少量点题短字。",
        ),
        (
            "信息拆解图",
            "把文案中的关键观点拆成 3 到 5 个信息块，用圆角矩形、对话气泡、箭头或虚线连接。",
        ),
        (
            "氛围场景图",
            "根据文案内容设计温暖、轻松、真实的小红书生活化场景，画面干净，有情绪但不过度拥挤。",
        ),
    ]
    prompts = []
    for label, instruction in variants:
        prompts.append(
            "\n".join(
                [
                    "你正在为小红书图文内容生成配图。",
                    f"请严格遵循以下风格框架：{style}",
                    f"文案标题：{title}",
                    f"文案摘要：{summary}",
                    f"核心话题：{topics}",
                    f"本张图片定位：{label}",
                    f"画面要求：{instruction}",
                    "请输出适合小红书发布的竖版视觉，3:4 构图，主体突出，画面干净。",
                    "可以有极少量点题短字，但不要大段文字，不要 logo，不要水印，不要二维码，不要截图拼贴感。",
                    "不要沿用示例主题，不要出现与当前文案无关的主体、道具或场景。",
                ]
            )
        )
    return prompts


mcp = FastMCP("xhs-content-image")


@mcp.tool()
async def collect_xhs_benchmark_notes(
    keyword: str,
    output_path: str | None = None,
    page: int = 1,
) -> dict[str, Any]:
    """按关键词搜索小红书图文笔记，按默认综合数据选前 3 篇，提取标题、正文、话题并保存 Markdown。"""
    return await XHSClient().collect_top_notes(
        keyword=keyword,
        output_path=output_path,
        page=page,
    )


@mcp.tool()
async def generate_xhs_style_images(
    markdown_path: str | None = None,
    output_dir: str | None = None,
    model: str = DEFAULT_SEEDREAM_MODEL,
    size: str = "1664x2240",
) -> dict[str, Any]:
    """读取对标内容 Markdown，使用 Doubao-Seedream-5.0-lite 生成 3 张小红书风格 PNG 配图。"""
    return await XHSClient().generate_xhs_images(
        markdown_path=markdown_path,
        output_dir=output_dir,
        model=model,
        size=size,
    )


async def run_collect_test(keyword: str) -> None:
    result = await XHSClient().collect_top_notes(keyword)
    print(json.dumps(result, ensure_ascii=False, indent=2))


async def run_image_test(markdown_path: str | None) -> None:
    result = await XHSClient().generate_xhs_images(markdown_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="小红书关键词对标内容与配图 MCP 服务")
    parser.add_argument("--test-keyword", help="运行一次关键词收集测试，不启动 MCP")
    parser.add_argument("--test-images", nargs="?", const="", help="运行一次配图测试，可传 Markdown 路径")
    args = parser.parse_args()

    if args.test_keyword:
        asyncio.run(run_collect_test(args.test_keyword))
    elif args.test_images is not None:
        asyncio.run(run_image_test(args.test_images or None))
    else:
        mcp.run()


if __name__ == "__main__":
    main()

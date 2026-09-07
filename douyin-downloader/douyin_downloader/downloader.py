from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlparse

import httpx
import imageio_ffmpeg
from playwright.async_api import Browser, async_playwright

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "downloads"
ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')
DETAIL_ID_PATTERNS = (
    re.compile(r"https?://www\.douyin\.com/(?:video|note|slides)/([0-9]{19})"),
    re.compile(r"https?://www\.iesdouyin\.com/share/(?:video|note|slides)/([0-9]{19})"),
    re.compile(r"\bmodal_id=([0-9]{19})\b"),
    re.compile(r"\b([0-9]{19})\b"),
)


class DouyinDownloadError(RuntimeError):
    """Raised when a public Douyin video cannot be parsed or downloaded."""


@dataclass(slots=True)
class MediaStream:
    kind: str
    url: str
    content_length: int


@dataclass(slots=True)
class ParsedVideo:
    aweme_id: str
    title: str
    author: str
    create_time: str
    download_url: str
    audio_url: str
    page_url: str


def safe_filename(value: str, fallback: str, max_length: int = 120) -> str:
    cleaned = ILLEGAL_FILENAME_CHARS.sub("_", value).strip(" ._")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:max_length].strip(" ._") or fallback


def extract_detail_id(text: str) -> str | None:
    for pattern in DETAIL_ID_PATTERNS:
        if match := pattern.search(text):
            return match.group(1)
    return None


def strip_site_suffix(title: str) -> str:
    return re.sub(r"\s+-\s+(?:抖音|Douyin)\s*$", "", title).strip()


def unique_output_path(root: Path, filename: str) -> Path:
    path = root / filename
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = root / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    raise DouyinDownloadError(f"无法生成不重名的输出文件: {path}")


def browser_executable() -> str | None:
    configured = os.environ.get("DOUYIN_BROWSER_PATH")
    if configured and Path(configured).is_file():
        return configured

    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    return next((str(path) for path in candidates if path.is_file()), None)


class DouyinClient:
    def __init__(
        self,
        timeout: float = 60.0,
        capture_timeout: float = 25.0,
        proxy: str | None = None,
    ):
        self.timeout = timeout
        self.capture_timeout = capture_timeout
        self.proxy = proxy or os.environ.get("DOUYIN_PROXY")

    async def _launch_browser(self, playwright) -> Browser:
        launch_options: dict[str, Any] = {"headless": True}
        executable = browser_executable()
        if executable:
            launch_options["executable_path"] = executable
        if self.proxy:
            launch_options["proxy"] = {"server": self.proxy}
        try:
            return await playwright.chromium.launch(**launch_options)
        except Exception as exc:
            raise DouyinDownloadError(
                "无法启动 Chrome/Edge。请安装浏览器，或设置 DOUYIN_BROWSER_PATH；"
                "若使用 Playwright 自带浏览器，请运行 python -m playwright install chromium。"
            ) from exc

    async def _capture_media(self, url: str) -> ParsedVideo:
        streams: dict[str, MediaStream] = {}
        page_url = url
        title = ""
        author = ""

        async with async_playwright() as playwright:
            browser = await self._launch_browser(playwright)
            try:
                context = await browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1536, "height": 864},
                    locale="zh-CN",
                )
                page = await context.new_page()

                async def on_response(response) -> None:
                    try:
                        content_type = response.headers.get("content-type", "")
                        media_url = response.url
                        if not re.search(r"^(?:video|audio)/mp4", content_type, re.I):
                            return
                        if not re.search(r"media-(?:video|audio)", media_url, re.I):
                            return
                        kind = "audio" if "media-audio" in media_url.lower() else "video"
                        content_length = int(response.headers.get("content-length", "0") or 0)
                        current = streams.get(kind)
                        if current is None or content_length >= current.content_length:
                            streams[kind] = MediaStream(kind, media_url, content_length)
                    except (ValueError, TypeError):
                        return

                page.on("response", on_response)
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                except Exception as exc:
                    raise DouyinDownloadError(f"打开抖音链接失败: {exc}") from exc

                deadline = monotonic() + self.capture_timeout
                while monotonic() < deadline and "video" not in streams:
                    await page.wait_for_timeout(250)

                page_url = page.url
                title = strip_site_suffix(await page.title())
                try:
                    authors = await page.locator('a[href*="/user/"]').all_inner_texts()
                    author = next((item.strip() for item in authors if item.strip()), "")
                except Exception:
                    author = ""
            finally:
                await browser.close()

        aweme_id = extract_detail_id(page_url) or extract_detail_id(url)
        video = streams.get("video")
        audio = streams.get("audio")
        if not aweme_id:
            raise DouyinDownloadError("无法从抖音链接中提取作品 ID。")
        if video is None:
            raise DouyinDownloadError(
                "未捕获到视频媒体流。该作品可能已删除、需要登录，或触发了抖音验证。"
            )

        return ParsedVideo(
            aweme_id=aweme_id,
            title=safe_filename(title or aweme_id, aweme_id),
            author=author,
            create_time="",
            download_url=video.url,
            audio_url=audio.url if audio else "",
            page_url=page_url,
        )

    async def parse(self, url: str) -> ParsedVideo:
        if not url or not url.startswith(("http://", "https://")):
            raise DouyinDownloadError("请输入有效的抖音 URL。")
        return await self._capture_media(url)

    async def _download_stream(self, url: str, path: Path, referer: str) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        headers = {
            "Accept": "*/*",
            "Referer": referer,
            "User-Agent": USER_AGENT,
        }
        async with httpx.AsyncClient(
            follow_redirects=True,
            verify=False,
            timeout=self.timeout,
            proxy=self.proxy,
        ) as client:
            async with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                with path.open("wb") as output:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        output.write(chunk)
                        total += len(chunk)
        if total == 0:
            raise DouyinDownloadError(f"下载到的媒体流为空: {url}")
        return total

    @staticmethod
    def _merge_streams(video_path: Path, audio_path: Path, output_path: Path) -> None:
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        command = [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-shortest",
            str(output_path),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return

        fallback = [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            str(output_path),
        ]
        result = subprocess.run(fallback, capture_output=True, text=True, check=False)
        if result.returncode:
            raise DouyinDownloadError(
                f"合并音视频失败: {(result.stderr or 'ffmpeg failed').strip()}"
            )

    async def download(self, url: str, output_dir: str | None = None) -> dict[str, Any]:
        video = await self.parse(url)
        root = Path(output_dir).expanduser() if output_dir else DEFAULT_OUTPUT_DIR
        root.mkdir(parents=True, exist_ok=True)
        output_path = unique_output_path(root, f"{video.title}.mp4")

        with tempfile.TemporaryDirectory(prefix="douyin-mcp-") as temp_dir:
            temp_root = Path(temp_dir)
            video_path = temp_root / "video.mp4"
            audio_path = temp_root / "audio.mp4"
            await self._download_stream(video.download_url, video_path, video.page_url)
            if video.audio_url:
                await self._download_stream(video.audio_url, audio_path, video.page_url)
                await asyncio.to_thread(self._merge_streams, video_path, audio_path, output_path)
            else:
                shutil.copyfile(video_path, output_path)

        size = output_path.stat().st_size
        return {
            "aweme_id": video.aweme_id,
            "type": "video",
            "title": video.title,
            "author": video.author,
            "create_time": video.create_time,
            "download_url": video.download_url,
            "audio_url": video.audio_url,
            "page_url": video.page_url,
            "file": {"path": str(output_path.resolve()), "bytes": size},
        }

    async def download_many(
        self,
        urls: list[str],
        output_dir: str | None = None,
    ) -> dict[str, Any]:
        root = Path(output_dir).expanduser() if output_dir else DEFAULT_OUTPUT_DIR
        root.mkdir(parents=True, exist_ok=True)
        items: list[dict[str, Any]] = []
        success = 0
        for index, url in enumerate(urls, start=1):
            item: dict[str, Any] = {"index": index, "url": url, "ok": False}
            try:
                item.update(await self.download(url, str(root)))
                item["ok"] = True
                success += 1
            except Exception as exc:
                item["error"] = str(exc)
            items.append(item)
        return {
            "output_dir": str(root.resolve()),
            "total": len(items),
            "success": success,
            "failed": len(items) - success,
            "items": items,
        }


def looks_like_douyin_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("douyin.com") or host.endswith("iesdouyin.com")

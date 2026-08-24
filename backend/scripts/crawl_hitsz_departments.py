"""Crawl public HITSZ department articles into department_files.

The crawler is deliberately conservative:
- only follows links on the configured department host;
- honors robots.txt when one is present;
- rate-limits requests and retries transient failures;
- saves clean Markdown plus a JSONL manifest;
- supports resume/deduplication by source URL and content hash.

Example:
    python -m scripts.crawl_hitsz_departments \
        --output ../department_files --target 100 --delay 1.2
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
import unicodedata
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup, Tag


USER_AGENT = "WenshuRAGCrawler/1.0 (public academic website archiver)"

# These names intentionally match ingest_department_files.DEPT_MAP.
DEPARTMENTS: tuple[tuple[str, str], ...] = (
    ("教务处", "http://due.hitsz.edu.cn/"),
    ("学生处", "http://xg.hitsz.edu.cn/"),
    ("财务处", "http://cws.hitsz.edu.cn/"),
    ("人事处", "http://hr.hitsz.edu.cn/"),
    ("后勤与安全保卫部", "http://ssd.hitsz.edu.cn/"),
)

ARTICLE_PATH_RE = re.compile(r"/info/\d+/\d+\.htm$", re.I)
DATE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[年./\-](\d{1,2})[月./\-](\d{1,2})日?")
SKIP_PATH_PARTS = (
    "/_upload/", "/system/", "/search/", "/login/", "/admin/",
    "/en/", "/english/",
)
PAGE_SUFFIXES = (".htm", ".html", "/")
CONTENT_SELECTORS = (
    ".v_news_content", "#vsb_content", "#vsb_content_2",
    ".wp_articlecontent", ".article-content", ".article_content",
    ".articleContent", ".content", "article",
)
TITLE_SELECTORS = (
    ".arti_title", ".article-title", ".article_title",
    ".news_title", ".content-title", ".tit",
)
SITE_TITLE_SUFFIX_HINTS = (
    "哈尔滨工业大学", "学生工作部", "教务部", "财务处",
    "人力资源处", "安全保卫部", "总务处",
)


@dataclass(frozen=True)
class SavedDocument:
    department: str
    title: str
    source_url: str
    published_at: str
    crawled_at: str
    content_sha256: str
    relative_path: str
    char_count: int
    insecure_transport: bool
    attachments: list[str]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_space(value: str) -> str:
    return re.sub(r"[ \t\u00a0\u3000]+", " ", value).strip()


def safe_filename(value: str, limit: int = 90) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" ._") or "untitled"
    return value[:limit].rstrip(" ._")


def canonicalize(raw_url: str, base_url: str, allowed_host: str, scheme: str) -> str | None:
    absolute = urljoin(base_url, raw_url)
    parts = urlsplit(absolute)
    if parts.scheme not in {"http", "https"} or parts.hostname != allowed_host:
        return None
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if any(part in path.lower() for part in SKIP_PATH_PARTS):
        return None
    if not (ARTICLE_PATH_RE.search(path) or path.lower().endswith(PAGE_SUFFIXES)):
        return None
    # Department HTTPS endpoints currently expose an incompatible certificate
    # chain on some Windows hosts. Preserve the configured seed scheme.
    return urlunsplit((scheme, allowed_host, path, "", ""))


def is_article(url: str) -> bool:
    return bool(ARTICLE_PATH_RE.search(urlsplit(url).path))


def load_manifest(path: Path) -> list[SavedDocument]:
    if not path.exists():
        return []
    rows: list[SavedDocument] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(SavedDocument(**json.loads(line)))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return rows


def write_manifest(path: Path, rows: Iterable[SavedDocument]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    payload = "".join(json.dumps(asdict(row), ensure_ascii=False) + "\n" for row in rows)
    temp.write_text(payload, encoding="utf-8")
    temp.replace(path)


class PoliteClient:
    def __init__(self, delay: float, timeout: float, allow_insecure_tls: bool) -> None:
        self.delay = max(delay, 0.5)
        self.timeout = timeout
        self.allow_insecure_tls = allow_insecure_tls
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            timeout=timeout,
            verify=not allow_insecure_tls,
            follow_redirects=False,
        )

    def close(self) -> None:
        self.client.close()

    def _wait(self, host: str) -> None:
        elapsed = time.monotonic() - self._last_request.get(host, 0.0)
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def _request_once(self, url: str) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        self._wait(host)
        try:
            return self.client.get(url)
        finally:
            self._last_request[host] = time.monotonic()

    def _robots_for(self, scheme: str, host: str) -> RobotFileParser | None:
        key = f"{scheme}://{host}"
        if key in self._robots:
            return self._robots[key]
        robots_url = f"{key}/robots.txt"
        try:
            response = self._request_once(robots_url)
            if response.status_code == 200:
                parser = RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(response.text.splitlines())
                self._robots[key] = parser
            else:
                self._robots[key] = None
        except httpx.HTTPError:
            # A missing/unreachable robots file is not treated as permission to
            # leave the configured public host; host restrictions still apply.
            self._robots[key] = None
        return self._robots[key]

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        parser = self._robots_for(parts.scheme, parts.hostname or "")
        return parser is None or parser.can_fetch(USER_AGENT, url)

    def get(self, url: str, allowed_host: str, retries: int = 2) -> httpx.Response | None:
        if not self.allowed(url):
            print(f"[robots] skip {url}")
            return None
        current = url
        for attempt in range(retries + 1):
            try:
                for _ in range(5):
                    response = self._request_once(current)
                    if response.status_code not in {301, 302, 303, 307, 308}:
                        break
                    target = urljoin(current, response.headers.get("location", ""))
                    if urlsplit(target).hostname != allowed_host:
                        return None
                    current = target
                if response.status_code == 200:
                    content_type = response.headers.get("content-type", "").lower()
                    if "html" not in content_type:
                        return None
                    return response
                if response.status_code not in {429, 500, 502, 503, 504}:
                    return None
            except httpx.HTTPError as exc:
                if attempt >= retries:
                    print(f"[error] {url}: {exc}")
                    return None
            time.sleep(2 ** attempt)
        return None


def choose_title(soup: BeautifulSoup) -> str:
    # Legacy department sites consistently expose a clean article title in
    # <title>, while their first <h1> may be a breadcrumb such as "首页".
    if soup.title:
        title = normalize_space(soup.title.get_text(" ", strip=True))
        while "-" in title:
            head, suffix = title.rsplit("-", 1)
            if any(hint in suffix for hint in SITE_TITLE_SUFFIX_HINTS):
                title = head.strip()
                continue
            break
        if 2 <= len(title) <= 180:
            return title
    for selector in TITLE_SELECTORS:
        node = soup.select_one(selector)
        if node:
            title = normalize_space(node.get_text(" ", strip=True))
            if 2 <= len(title) <= 180:
                return title
    return "未命名文档"


def choose_content(soup: BeautifulSoup) -> Tag | None:
    candidates: list[Tag] = []
    for selector in CONTENT_SELECTORS:
        candidates.extend(node for node in soup.select(selector) if isinstance(node, Tag))
    if not candidates and soup.body:
        candidates.append(soup.body)
    if not candidates:
        return None
    return max(candidates, key=lambda node: len(normalize_space(node.get_text(" ", strip=True))))


def block_lines(container: Tag, title: str) -> list[str]:
    for node in container.select("script,style,noscript,nav,footer,form,iframe"):
        node.decompose()
    blocks = container.find_all(["h1", "h2", "h3", "h4", "p", "li", "tr"])
    raw_lines: list[str] = []
    for node in blocks:
        text = normalize_space(node.get_text(" ", strip=True))
        if not text or text == title:
            continue
        if node.name == "li":
            text = "- " + text
        elif node.name in {"h1", "h2", "h3", "h4"}:
            text = "## " + text
        raw_lines.append(text)
    if not raw_lines:
        raw_lines = [normalize_space(line) for line in container.get_text("\n").splitlines()]

    lines: list[str] = []
    seen_adjacent = ""
    boilerplate = {"打印", "关闭", "返回顶部", "上一篇", "下一篇", "责任编辑"}
    for line in raw_lines:
        if not line or line in boilerplate or line == seen_adjacent:
            continue
        if len(line) == 1 and not line.isalnum():
            continue
        lines.append(line)
        seen_adjacent = line
    return lines


def published_date(soup: BeautifulSoup) -> str:
    text = normalize_space(soup.get_text(" ", strip=True))
    match = DATE_RE.search(text)
    if not match:
        return ""
    year, month, day = (int(value) for value in match.groups())
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return ""


def attachment_links(container: Tag, base_url: str, allowed_host: str) -> list[str]:
    result: list[str] = []
    for anchor in container.select("a[href]"):
        target = urljoin(base_url, anchor.get("href", ""))
        parts = urlsplit(target)
        if parts.hostname != allowed_host:
            continue
        if Path(parts.path).suffix.lower() in {".pdf", ".doc", ".docx", ".xls", ".xlsx"}:
            clean = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            if clean not in result:
                result.append(clean)
    return result


def parse_article(html: str, url: str, department: str) -> tuple[SavedDocument, str] | None:
    soup = BeautifulSoup(html, "html.parser")
    title = choose_title(soup)
    container = choose_content(soup)
    if container is None:
        return None
    lines = block_lines(container, title)
    content = "\n\n".join(lines).strip()
    if len(content) < 180:
        return None
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    parts = urlsplit(url)
    attachments = attachment_links(container, url, parts.hostname or "")
    crawled = utc_now()
    published = published_date(soup)
    header = [
        "---",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"department: {json.dumps(department, ensure_ascii=False)}",
        f"source_url: {json.dumps(url, ensure_ascii=False)}",
        f"published_at: {json.dumps(published, ensure_ascii=False)}",
        f"crawled_at: {json.dumps(crawled, ensure_ascii=False)}",
        f"content_sha256: {json.dumps(digest, ensure_ascii=False)}",
        "---",
        "",
        f"# {title}",
        "",
        content,
        "",
    ]
    markdown = "\n".join(header)
    placeholder = SavedDocument(
        department=department,
        title=title,
        source_url=url,
        published_at=published,
        crawled_at=crawled,
        content_sha256=digest,
        relative_path="",
        char_count=len(content),
        insecure_transport=parts.scheme == "http",
        attachments=attachments,
    )
    return placeholder, markdown


def discover_links(soup: BeautifulSoup, base_url: str, allowed_host: str, scheme: str) -> list[str]:
    links: list[str] = []
    for anchor in soup.select("a[href]"):
        target = canonicalize(anchor.get("href", ""), base_url, allowed_host, scheme)
        if target and target not in links:
            links.append(target)
    # Prefer articles, then list/index pages, while keeping the page's order.
    return sorted(links, key=lambda value: (not is_article(value), value))


def crawl_department(
    client: PoliteClient,
    department: str,
    seed_url: str,
    output: Path,
    limit: int,
    max_pages: int,
    known_urls: set[str],
    known_hashes: set[str],
) -> list[SavedDocument]:
    parts = urlsplit(seed_url)
    host = parts.hostname or ""
    scheme = parts.scheme
    queue: deque[str] = deque([seed_url])
    visited: set[str] = set()
    saved: list[SavedDocument] = []
    department_dir = output / department
    department_dir.mkdir(parents=True, exist_ok=True)

    while queue and len(visited) < max_pages and len(saved) < limit:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        response = client.get(url, allowed_host=host)
        if response is None:
            continue
        soup = BeautifulSoup(response.text, "html.parser")
        for link in discover_links(soup, url, host, scheme):
            if link not in visited and link not in queue:
                queue.append(link)

        if not is_article(url) or url in known_urls:
            continue
        parsed = parse_article(response.text, url, department)
        if parsed is None:
            continue
        document, markdown = parsed
        if document.content_sha256 in known_hashes:
            known_urls.add(url)
            continue
        short_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        date_prefix = document.published_at or "undated"
        filename = f"{date_prefix}_{safe_filename(document.title)}_{short_hash}.md"
        destination = department_dir / filename
        temp = destination.with_suffix(".md.tmp")
        temp.write_text(markdown, encoding="utf-8")
        temp.replace(destination)
        document = SavedDocument(
            **{
                **asdict(document),
                "relative_path": destination.relative_to(output).as_posix(),
            }
        )
        saved.append(document)
        known_urls.add(url)
        known_hashes.add(document.content_sha256)
        print(f"[saved] {department}: {document.title} ({document.char_count} chars)")

    print(f"[department] {department}: +{len(saved)}, scanned={len(visited)}")
    return saved


def run(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "_hitsz_manifest.jsonl"
    rows = load_manifest(manifest_path)
    known_urls = {row.source_url for row in rows}
    known_hashes = {row.content_sha256 for row in rows}
    existing_count = len(rows)
    remaining = max(args.target - existing_count, 0)
    if remaining == 0:
        print(f"目标已满足: manifest 中已有 {existing_count} 篇")
        return 0

    selected = [item for item in DEPARTMENTS if not args.departments or item[0] in args.departments]
    if not selected:
        raise SystemExit("没有匹配的部门")
    client = PoliteClient(args.delay, args.timeout, args.allow_insecure_tls)
    try:
        soft_quota = math.ceil(remaining / len(selected))
        for department, seed in selected:
            if len(rows) >= args.target:
                break
            added = crawl_department(
                client, department, seed, output,
                limit=min(soft_quota, args.target - len(rows)),
                max_pages=args.max_pages_per_department,
                known_urls=known_urls,
                known_hashes=known_hashes,
            )
            rows.extend(added)
            write_manifest(manifest_path, rows)

        # Redistribute unused quota to departments with more public articles.
        if len(rows) < args.target:
            for department, seed in selected:
                if len(rows) >= args.target:
                    break
                added = crawl_department(
                    client, department, seed, output,
                    limit=args.target - len(rows),
                    max_pages=args.max_pages_per_department,
                    known_urls=known_urls,
                    known_hashes=known_hashes,
                )
                rows.extend(added)
                write_manifest(manifest_path, rows)
    finally:
        client.close()

    summary = {
        "target": args.target,
        "saved_total": len(rows),
        "new_in_this_run": len(rows) - existing_count,
        "generated_at": utc_now(),
        "source": "public HITSZ department websites",
        "departments": {
            name: sum(1 for row in rows if row.department == name)
            for name, _ in selected
        },
    }
    (output / "_hitsz_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if len(rows) >= args.target else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="采集哈工大（深圳）公开部门文章并保存为 Markdown")
    parser.add_argument("--output", default="../department_files", help="输出目录")
    parser.add_argument("--target", type=int, default=100, help="目标文档总数（默认 100）")
    parser.add_argument("--delay", type=float, default=1.2, help="同一站点请求间隔秒数，最小 0.5")
    parser.add_argument("--timeout", type=float, default=20.0, help="单请求超时秒数")
    parser.add_argument("--max-pages-per-department", type=int, default=500, help="每部门最多扫描页面数")
    parser.add_argument("--departments", nargs="*", default=[], help="只采集指定部门名称")
    parser.add_argument("--allow-insecure-tls", action="store_true", help="显式允许旧站 HTTPS 证书问题")
    return parser


if __name__ == "__main__":
    raise SystemExit(run(build_parser().parse_args()))

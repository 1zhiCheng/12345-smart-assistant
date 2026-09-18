"""采集芜湖市官方部门网站公开文件，构建 12345 十二类别知识库。

仅访问 *.wuhu.gov.cn 官方站点，遵守 robots.txt、限速、同域抓取；正文保存为
Markdown，清单保存为 JSONL，支持按 URL 与正文哈希增量去重。

示例：
    python -m scripts.crawl_wuhu_knowledge --output ../wuhu_knowledge_base --per-category 15
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
import unicodedata
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup, Tag


USER_AGENT = "Wuhu12345KnowledgeBot/1.0 (public-government-document-research)"
OFFICIAL_SUFFIX = ".wuhu.gov.cn"
ARTICLE_RE = re.compile(r"(?:/\d{6,}\.(?:html?|shtml)|/public/(?:content/)?\d+|/openness/public/\d+/\d+\.html)$", re.I)
DATE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?")


@dataclass(frozen=True)
class Source:
    category: str
    lead_department: str
    department: str
    seed_url: str


SOURCES = (
    Source("城市管理", "芜湖市城市管理局", "芜湖市城市管理局", "https://cgj.wuhu.gov.cn/public/column/6596781?catIds=6732121%2C6732131&type=6"),
    Source("城乡建设", "芜湖市住房和城乡建设局", "芜湖市住房和城乡建设局", "https://zjw.wuhu.gov.cn/public/column/6596411?catIds=6732121%2C6732131&type=6"),
    Source("公共安全", "芜湖市公安局", "芜湖市公安局", "https://gaj.wuhu.gov.cn/public/column/6596351?catIds=6732121%2C6732131&type=6"),
    Source("公共安全", "芜湖市公安局", "芜湖市公安局", "https://gaj.wuhu.gov.cn/jwzx/jfts/index.html"),
    Source("公共安全", "芜湖市公安局", "芜湖市公安局", "https://gaj.wuhu.gov.cn/jwzx/xwfb/index.html"),
    Source("公共服务", "芜湖市民政局", "芜湖市民政局", "https://mzj.wuhu.gov.cn/public/column/6596361?catIds=6732121%2C6732131&type=6"),
    Source("公共服务", "芜湖市民政局", "芜湖市数据资源管理局", "https://sjzyj.wuhu.gov.cn/public/column/6596821?type=4&catId=6732121&action=list"),
    Source("交通运输", "芜湖市交通运输局", "芜湖市交通运输局", "https://jtj.wuhu.gov.cn/public/column/6596421?catIds=6732121%2C6732131&type=6"),
    Source("交通运输", "芜湖市交通运输局", "芜湖市交通运输局", "https://jtj.wuhu.gov.cn/ztzl/yhyshj/zcwj/index.html"),
    Source("经济财贸", "芜湖市发展和改革委员会", "芜湖市发展和改革委员会", "https://whfgw.wuhu.gov.cn/public/column/6596311?catIds=6732121%2C6732131&type=6"),
    Source("经济财贸", "芜湖市发展和改革委员会", "芜湖市发展和改革委员会", "https://whfgw.wuhu.gov.cn/fgqd/sqsfqd/index.html"),
    Source("经济财贸", "芜湖市发展和改革委员会", "芜湖市商务局", "https://swj.wuhu.gov.cn/public/column/6596631?type=4&catId=6732121&action=list"),
    Source("经济财贸", "芜湖市发展和改革委员会", "芜湖市财政局", "https://czj.wuhu.gov.cn/public/column/6596341?type=4&action=list"),
    Source("科教文体", "芜湖市教育局", "芜湖市教育局", "https://jyj.wuhu.gov.cn/public/column/6596321?type=4&action=list"),
    Source("科教文体", "芜湖市教育局", "芜湖市科学技术局", "https://kjj.wuhu.gov.cn/public/column/6596331?type=4&catId=6732121&action=list"),
    Source("科教文体", "芜湖市教育局", "芜湖市文化和旅游局", "https://ct.wuhu.gov.cn/public/column/6596641?type=4&catId=6732121&action=list"),
    Source("科教文体", "芜湖市教育局", "芜湖市体育局", "https://tyj.wuhu.gov.cn/public/column/6596731?type=4&action=list"),
    Source("劳动和社会保障", "芜湖市人力资源和社会保障局", "芜湖市人力资源和社会保障局", "https://rsj.wuhu.gov.cn/ztzl/yhyshj/fwzn/index.html"),
    Source("劳动和社会保障", "芜湖市人力资源和社会保障局", "芜湖市医疗保障局", "https://www.wuhu.gov.cn/openness/szfzwgkzt/zcwjk/index.html?type=4&name=%E5%B8%82%E5%8C%BB%E4%BF%9D%E5%B1%80"),
    Source("农林水土", "芜湖市农业农村局", "芜湖市农业农村局", "https://nync.wuhu.gov.cn/public/column/6596611?catIds=6732121%2C6732131&type=6"),
    Source("农林水土", "芜湖市农业农村局", "芜湖市农业农村局", "https://nync.wuhu.gov.cn/public/column/6596611?type=4&catId=6732111&action=list"),
    Source("农林水土", "芜湖市农业农村局", "芜湖市水务局", "https://shuiwu.wuhu.gov.cn/public/column/6596671?type=4&action=list"),
    Source("农林水土", "芜湖市农业农村局", "芜湖市自然资源和规划局（林业局）", "https://zrzyhghj.wuhu.gov.cn/public/column/6596701?type=4&catId=7049758&action=list&nav=3"),
    Source("生态环境", "芜湖市生态环境局", "芜湖市生态环境局", "https://sthjj.wuhu.gov.cn/hbyw/fgbz/dfxfg/index.html"),
    Source("生态环境", "芜湖市生态环境局", "芜湖市生态环境局", "https://sthjj.wuhu.gov.cn/hbyw/hjzl/wrwpf/index.html"),
    Source("生态环境", "芜湖市生态环境局", "芜湖市生态环境局", "https://sthjj.wuhu.gov.cn/hbyw/hjzl/hjzlgb/index.html"),
    Source("市场监管", "芜湖市市场监督管理局", "芜湖市市场监督管理局", "https://amr.wuhu.gov.cn/public/column/6596721?catIds=6732121%2C6732131&type=6"),
    Source("卫生健康", "芜湖市卫生健康委员会", "芜湖市卫生健康委员会", "https://wsjkw.wuhu.gov.cn/public/column/6596651?catIds=6732121%2C6732131&type=6"),
)

CATEGORY_KEYWORDS = {
    "城市管理": ("城管", "市容", "环卫", "垃圾", "占道", "违建", "园林", "路灯", "广告", "渣土"),
    "城乡建设": ("住房", "物业", "建筑", "施工", "燃气", "供水", "房屋", "建设工程", "公积金", "老旧小区"),
    "公共安全": ("公安", "治安", "户口", "身份证", "居住证", "养犬", "反诈", "交通安全", "出入境", "报警"),
    "公共服务": ("民政", "社会救助", "低保", "养老", "殡葬", "婚姻登记", "残疾", "社区", "政务服务", "便民"),
    "交通运输": ("交通", "公交", "出租车", "道路运输", "客运", "货运", "驾培", "维修", "公路", "网约车"),
    "经济财贸": ("价格", "收费", "消费", "补贴", "企业", "营商", "商务", "财政", "税费", "粮食"),
    "科教文体": ("教育", "学校", "招生", "入学", "学籍", "科技", "文化", "旅游", "体育", "培训机构"),
    "劳动和社会保障": ("就业", "社保", "养老保险", "工伤", "失业", "劳动", "工资", "人才", "医保", "生育保险"),
    "农林水土": ("农业", "农村", "农民", "林业", "耕地", "水利", "河湖", "防汛", "宅基地", "渔业"),
    "生态环境": ("生态环境", "污染", "噪声", "扬尘", "排污", "环评", "水质", "大气", "固废", "环保"),
    "市场监管": ("市场监管", "12315", "消费维权", "食品安全", "药品", "价格违法", "广告", "特种设备", "营业执照", "质量"),
    "卫生健康": ("卫生健康", "医疗", "医院", "医师", "护士", "托育", "生育", "疾控", "公共卫生", "职业健康"),
}
DOCUMENT_WORDS = ("条例", "办法", "规定", "细则", "指南", "须知", "政策", "通知", "意见", "实施", "问答", "解读", "流程", "标准", "清单", "办理", "职责", "投诉", "维权", "提示", "提醒", "通告", "公报", "排放情况", "规范性文件", "机构简介")
SECTION_WORDS = ("政务公开", "政策法规", "法律法规", "法规标准", "法规及标准", "政策文件", "部门文件", "规范性文件", "政策解读", "办事指南", "政务指南", "服务指南", "通知公告", "热点问答", "下载专区", "专题专栏", "机构概况", "机构简介", "权责清单")
EXCLUDE_WORDS = (
    "招聘", "采购", "中标", "成交", "会议召开", "专题学习", "领导调研", "资格复审", "体检", "拟聘用", "工作动态", "简报",
    "竞赛", "比赛", "评选", "获奖", "名单", "公示", "审查意见", "审批结果", "验收结果", "征集作品", "项目申报",
    "预算", "决算", "三公经费", "财政拨款", "招标", "采购", "询价", "竞争性磋商", "成交结果", "服务项目",
)
CONTENT_SELECTORS = (
    ".j-fontContent", ".gkdhb_contnet", ".xxgkcontent",
    ".v_news_content", "#vsb_content", "#vsb_content_2", ".wp_articlecontent",
    ".article-content", ".article_content", ".articleContent", ".TRS_Editor",
    ".guestbook-show", ".ls-message-info", ".content", "article",
)


@dataclass(frozen=True)
class Record:
    category: str
    lead_department: str
    department: str
    title: str
    published_at: str
    source_url: str
    crawled_at: str
    content_sha256: str
    relative_path: str
    char_count: int


def clean_space(value: str) -> str:
    return re.sub(r"[ \t\u00a0\u3000]+", " ", value).strip()


def safe_name(value: str, limit: int = 92) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value)
    return (re.sub(r"\s+", " ", value).strip(" ._") or "未命名")[:limit].rstrip(" ._")


def canonical(raw: str, base: str, host: str) -> str | None:
    parts = urlsplit(urljoin(base, raw))
    if parts.scheme not in {"http", "https"} or parts.hostname != host:
        return None
    if any(token in parts.path.lower() for token in ("/_upload/", "/search/", "/login/", "/english/")):
        return None
    if Path(parts.path).suffix.lower() not in {"", ".htm", ".html", ".shtml"}:
        return None
    return urlunsplit((parts.scheme, parts.netloc, re.sub(r"/{2,}", "/", parts.path or "/"), "", ""))


class Client:
    def __init__(self, delay: float, timeout: float) -> None:
        self.delay = max(0.4, delay)
        self.timeout = timeout
        self.last_request: dict[str, float] = {}
        self.robots: dict[str, RobotFileParser | None] = {}
        self.http = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self.http.close()

    def _wait(self, host: str) -> None:
        remain = self.delay - (time.monotonic() - self.last_request.get(host, 0.0))
        if remain > 0:
            time.sleep(remain)

    def _get_raw(self, url: str) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        self._wait(host)
        try:
            return self.http.get(url)
        finally:
            self.last_request[host] = time.monotonic()

    def _curl_text(self, url: str) -> str | None:
        """Windows 上部分政府站点与 OpenSSL 握手失败时回退到系统 curl/Schannel。"""
        host = urlsplit(url).hostname or ""
        if not (host == "wuhu.gov.cn" or host.endswith(OFFICIAL_SUFFIX)):
            return None
        self._wait(host)
        try:
            result = subprocess.run(
                ["curl.exe", "-sS", "--max-time", str(int(self.timeout)), "-A", USER_AGENT, url],
                capture_output=True,
                check=False,
                timeout=self.timeout + 5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        finally:
            self.last_request[host] = time.monotonic()
        if result.returncode != 0 or not result.stdout:
            return None
        for encoding in ("utf-8", "gb18030"):
            try:
                return result.stdout.decode(encoding)
            except UnicodeDecodeError:
                continue
        return result.stdout.decode("utf-8", errors="replace")

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc}"
        if root not in self.robots:
            if (parts.hostname or "").endswith(OFFICIAL_SUFFIX):
                robots_text = self._curl_text(root + "/robots.txt")
                if robots_text:
                    parser = RobotFileParser(root + "/robots.txt")
                    parser.parse(robots_text.splitlines())
                    self.robots[root] = parser
                else:
                    self.robots[root] = None
                return self.robots[root] is None or self.robots[root].can_fetch(USER_AGENT, url)
            try:
                response = self._get_raw(root + "/robots.txt")
                if response.status_code == 200:
                    parser = RobotFileParser(root + "/robots.txt")
                    parser.parse(response.text.splitlines())
                    self.robots[root] = parser
                else:
                    self.robots[root] = None
            except httpx.HTTPError:
                robots_text = self._curl_text(root + "/robots.txt")
                if robots_text:
                    parser = RobotFileParser(root + "/robots.txt")
                    parser.parse(robots_text.splitlines())
                    self.robots[root] = parser
                else:
                    self.robots[root] = None
        parser = self.robots[root]
        return parser is None or parser.can_fetch(USER_AGENT, url)

    def get_html(self, url: str) -> str | None:
        if not self.allowed(url):
            return None
        if (urlsplit(url).hostname or "").endswith(OFFICIAL_SUFFIX):
            curl_text = self._curl_text(url)
            return curl_text if curl_text and "<html" in curl_text[:2000].lower() else None
        for attempt in range(3):
            try:
                response = self._get_raw(url)
                if response.status_code == 200 and "html" in response.headers.get("content-type", "").lower():
                    response.encoding = response.encoding or "utf-8"
                    return response.text
                if response.status_code not in {429, 500, 502, 503, 504}:
                    return None
            except httpx.HTTPError:
                curl_text = self._curl_text(url)
                if curl_text and "<html" in curl_text[:2000].lower():
                    return curl_text
                return None
            time.sleep(2**attempt)
        return None


def page_title(soup: BeautifulSoup) -> str:
    for selector in ("h1", ".title", ".article-title", ".arti_title", ".news_title"):
        node = soup.select_one(selector)
        if node:
            title = clean_space(node.get_text(" ", strip=True))
            if 3 <= len(title) <= 180 and title not in {"首页", "政府信息公开"}:
                return title
    if soup.title:
        return re.split(r"[_-]芜湖市", clean_space(soup.title.get_text(" ", strip=True)))[0]
    return "未命名文档"


def extract_content(soup: BeautifulSoup, title: str) -> str:
    # 芜湖政务公开“机构职能”页把正文放在 j-fontContent 中，外围
    # xxgkcontent 更长但以索引元数据为主，不能再用“最大容器”覆盖它。
    preferred = soup.select_one(".j-fontContent, .gkdhb_contnet")
    candidates: list[Tag] = []
    for selector in CONTENT_SELECTORS:
        candidates.extend(node for node in soup.select(selector) if isinstance(node, Tag))
    container = preferred or (max(candidates, key=lambda node: len(node.get_text(" ", strip=True))) if candidates else soup.body)
    if container is None:
        return ""
    for node in container.select("script,style,noscript,nav,footer,form,iframe"):
        node.decompose()
    lines: list[str] = []
    for node in container.find_all(["h2", "h3", "h4", "p", "li", "tr"]):
        value = clean_space(node.get_text(" ", strip=True))
        if not value or value == title or value in {"打印", "关闭", "上一篇", "下一篇", "返回顶部"}:
            continue
        if node.name == "li":
            value = "- " + value
        elif node.name in {"h2", "h3", "h4"}:
            value = "## " + value
        if not lines or lines[-1] != value:
            lines.append(value)
    if not lines:
        lines = [clean_space(line) for line in container.get_text("\n").splitlines() if clean_space(line)]
    # 部分站点表格单元内部带 CRLF；Windows 文本写入会再次转换 LF，
    # 若不先统一会形成 CRCRLF，导致落盘正文与清单哈希不一致。
    return "\n\n".join(lines).replace("\r\n", "\n").replace("\r", "\n")


def published_at(soup: BeautifulSoup) -> str:
    match = DATE_RE.search(clean_space(soup.get_text(" ", strip=True)))
    if not match:
        return ""
    try:
        return datetime(*(int(x) for x in match.groups())).date().isoformat()
    except ValueError:
        return ""


def relevance(category: str, title: str, content: str) -> int:
    sample = f"{title} {content[:1800]}"
    if any(word in title for word in EXCLUDE_WORDS):
        return -10
    if not any(word in title for word in DOCUMENT_WORDS) and not re.search(r"(?:法|条例)$", title):
        return -10
    score = sum(2 for word in CATEGORY_KEYWORDS[category] if word in sample)
    score += sum(1 for word in DOCUMENT_WORDS if word in title)
    if any(word in content[:600] for word in ("发布机构", "文件", "第一条", "申请材料", "办理流程", "咨询电话")):
        score += 2
    return score


def load_records(path: Path) -> list[Record]:
    if not path.exists():
        return []
    rows: list[Record] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(Record(**json.loads(line)))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return rows


def write_manifest(path: Path, rows: list[Record]) -> None:
    path.write_text("".join(json.dumps(asdict(row), ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def collect_source(client: Client, source: Source, output: Path, target: int, max_pages: int, known_urls: set[str], known_hashes: set[str]) -> list[Record]:
    host = urlsplit(source.seed_url).hostname or ""
    queue = deque([(source.seed_url, 0)])
    visited: set[str] = set()
    saved: list[Record] = []
    while queue and len(visited) < max_pages and len(saved) < target:
        url, depth = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        html = client.get_html(url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        if ARTICLE_RE.search(urlsplit(url).path):
            title = page_title(soup)
            content = extract_content(soup, title)
            if len(content) >= 180 and relevance(source.category, title, content) >= 3:
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if url not in known_urls and digest not in known_hashes:
                    date = published_at(soup)
                    folder = output / source.category / source.department
                    folder.mkdir(parents=True, exist_ok=True)
                    filename = f"{date or '日期未知'}_{safe_name(title)}_{digest[:10]}.md"
                    file_path = folder / filename
                    header = (
                        f"# {title}\n\n"
                        f"- 事项类别：{source.category}\n"
                        f"- 牵头部门：{source.lead_department}\n"
                        f"- 发布部门：{source.department}\n"
                        f"- 发布日期：{date or '未识别'}\n"
                        f"- 来源：{url}\n"
                        f"- 采集时间：{datetime.now(timezone.utc).isoformat()}\n\n"
                    )
                    file_path.write_text(header + content.strip() + "\n", encoding="utf-8")
                    record = Record(source.category, source.lead_department, source.department, title, date, url, datetime.now(timezone.utc).isoformat(), digest, file_path.relative_to(output).as_posix(), len(content))
                    saved.append(record)
                    known_urls.add(url)
                    known_hashes.add(digest)
                    print(f"[保存] {source.category}/{source.department}: {title}", flush=True)
        if depth >= 3:
            continue
        section_links: list[str] = []
        article_links: list[str] = []
        for anchor in soup.select("a[href]"):
            link = canonical(anchor.get("href", ""), url, host)
            if not link or link in visited:
                continue
            label = clean_space(anchor.get_text(" ", strip=True))
            if ARTICLE_RE.search(urlsplit(link).path):
                # 政策栏目中的有效标题不一定含“办法/通知”等固定词；正文抓取后再由
                # relevance 统一判断，避免在链接发现阶段漏掉办事指南和政策问答。
                if label and not any(word in label for word in EXCLUDE_WORDS):
                    article_links.append(link)
            elif any(word in label for word in SECTION_WORDS):
                section_links.append(link)
        ordered_links = [*dict.fromkeys(section_links), *dict.fromkeys(article_links)]
        queue.extend((link, depth + 1) for link in ordered_links)
    print(f"[来源完成] {source.department}: 浏览 {len(visited)} 页，新增 {len(saved)} 篇", flush=True)
    return saved


def collect_curated(client: Client, seed_file: Path, output: Path, known_urls: set[str], known_hashes: set[str]) -> list[Record]:
    """下载经过人工检索确认的高价值官方页面，不再应用通用标题筛选。"""
    items = json.loads(seed_file.read_text(encoding="utf-8"))
    saved: list[Record] = []
    for item in items:
        url = item["url"]
        host = urlsplit(url).hostname or ""
        if not host.endswith(OFFICIAL_SUFFIX) or url in known_urls:
            continue
        html = client.get_html(url)
        if not html:
            print(f"[失败] {url}", flush=True)
            continue
        soup = BeautifulSoup(html, "html.parser")
        title = clean_space(item.get("title", "")) or page_title(soup)
        content = extract_content(soup, title)
        if len(content) < 180:
            print(f"[正文过短] {title}: {url}", flush=True)
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if digest in known_hashes:
            continue
        date = published_at(soup)
        folder = output / item["category"] / item["department"]
        folder.mkdir(parents=True, exist_ok=True)
        filename = f"{date or '日期未知'}_{safe_name(title)}_{digest[:10]}.md"
        file_path = folder / filename
        crawled = datetime.now(timezone.utc).isoformat()
        header = (
            f"# {title}\n\n"
            f"- 事项类别：{item['category']}\n"
            f"- 牵头部门：{item['lead_department']}\n"
            f"- 发布部门：{item['department']}\n"
            f"- 发布日期：{date or '未识别'}\n"
            f"- 来源：{url}\n"
            f"- 采集时间：{crawled}\n"
            f"- 质检状态：人工检索白名单\n\n"
        )
        file_path.write_text(header + content.strip() + "\n", encoding="utf-8")
        saved.append(Record(item["category"], item["lead_department"], item["department"], title, date, url, crawled, digest, file_path.relative_to(output).as_posix(), len(content)))
        known_urls.add(url)
        known_hashes.add(digest)
        print(f"[白名单保存] {item['category']}: {title}", flush=True)
    return saved


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="../wuhu_knowledge_base")
    parser.add_argument("--per-category", type=int, default=15)
    parser.add_argument("--max-pages", type=int, default=180)
    parser.add_argument("--delay", type=float, default=0.55)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--curated-file", type=Path, help="人工检索确认的 JSON URL 白名单；设置后不执行泛爬")
    args = parser.parse_args()

    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / "manifest.jsonl"
    records = load_records(manifest)
    known_urls = {row.source_url for row in records}
    known_hashes = {row.content_sha256 for row in records}
    counts = {category: sum(row.category == category for row in records) for category in CATEGORY_KEYWORDS}
    source_totals = {
        category: sum(source.category == category for source in SOURCES)
        for category in CATEGORY_KEYWORDS
    }
    source_seen: dict[str, int] = {category: 0 for category in CATEGORY_KEYWORDS}
    client = Client(args.delay, args.timeout)
    try:
        if args.curated_file:
            added = collect_curated(client, args.curated_file.resolve(), output, known_urls, known_hashes)
            records.extend(added)
            counts = {category: sum(row.category == category for row in records) for category in CATEGORY_KEYWORDS}
            write_manifest(manifest, records)
        else:
            for source in SOURCES:
                source_seen[source.category] += 1
                category_remaining = max(0, args.per_category - counts[source.category])
                if category_remaining == 0:
                    continue
                source_target = (args.per_category + source_totals[source.category] - 1) // source_totals[source.category]
                existing_for_source = sum(
                    row.category == source.category and row.department == source.department
                    for row in records
                )
                remaining_sources = source_totals[source.category] - source_seen[source.category] + 1
                fair_share = max(1, (category_remaining + remaining_sources - 1) // remaining_sources)
                target = min(category_remaining, max(max(0, source_target - existing_for_source), fair_share))
                added = collect_source(client, source, output, target, args.max_pages, known_urls, known_hashes)
                records.extend(added)
                counts[source.category] += len(added)
                write_manifest(manifest, records)
    finally:
        client.close()

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(records),
        "by_category": counts,
        "official_only": True,
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

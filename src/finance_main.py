"""财经资讯独立采集系统

专注于捕获科技媒体RSS源覆盖不到的财经事件：
港股IPO、上市暴涨、财报解读、产业政策、融资动态等。

独立于 ai-news-collector 的科技日报管线，互不干扰。
"""

import os
import re
import json
import time
import yaml
import hashlib
import feedparser
import shutil
import requests
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from loguru import logger
from bs4 import BeautifulSoup


# ============================================================
# 配置
# ============================================================

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(PROJECT_DIR, "reports")
OBSIDIAN_DIR = os.environ.get("OBSIDIAN_DIR", "")  # 设置环境变量指向你的 Obsidian 知识库路径，留空则跳过同步

# 财经RSS源
FINANCE_SOURCES = [
    {
        "name": "华尔街见闻",
        "url": "http://localhost:1200/wallstreetcn/news/global",
        "max_items": 50,
        "timeout": 60,
    },
    # CLS 深度源（签名验证变更导致不可用，待 RSSHub 社区修复后恢复为主源）:
    # {"name": "财联社深度", "url": "https://rsshub-balancer.virworks.moe/cls/depth", "max_items": 50, "timeout": 30},
    # 备用实例参考：rssforever / ktachibana / wudifeixue / umzzz / isrss / cups.moe / spriple
    # 仅文档记录，部分域名可能已被标记，实际使用请先验证安全性
]

# 必须命中的核心科技关键词（至少命中一个才保留）
CORE_TECH_KEYWORDS = [
    # AI
    "ai", "人工智能", "大模型", "gpt", "claude", "llm", "chatgpt",
    "智能体", "ai agent", "copilot", "aigc", "生成式", "anthropic", "deepseek",
    # 具身智能
    "机器人", "具身智能", "人形机器人", "机械臂",
    # 芯片半导体
    "芯片", "半导体", "gpu", "cpu", "npu", "算力",
    "台积电", "中芯国际", "英伟达", "nvidia",
    # 新能源/储能
    "新能源", "电动车", "电动汽车", "动力电池", "固态电池", "储能", "光储",
    "光伏", "宁德时代", "特斯拉", "蔚来", "理想", "小鹏", "比亚迪",
    "自动驾驶", "无人驾驶", "智驾",
    # 光通信
    "光模块", "cpo", "光通信", "中际旭创",
    # 低空经济
    "低空经济", "evtol", "飞行汽车",
    # 资本市场/科技
    "港股上市", "科创板", "ipo", "融资", "估值",
    # 科技巨头
    "华为", "阿里", "字节", "腾讯", "百度", "openai",
    "meta", "google", "微软", "苹果", "amazon",
    "智谱", "月之暗面", "kimi", "千问", "通义", "文心",
    # 量子
    "量子计算", "量子通信",
]

# 强排除模式（命中即排除，不看核心关键词）
EXCLUDE_PATTERNS = [
    r"\|速读公告",
    r"\|盘后公告集锦",
    r"退市风险警示.*触发",
    r"会计处理调整.*触发",
    r"可能被实施退市风险警示",
    # 纯宏观/消费/地缘
    r"五一.*出游|假期.*预订",
    r"黄金.*大涨|金价.*上涨",
    r"红海航道|美伊.*和平|美伊.*封锁",
    r"房价.*信号|二手房复苏",
    r"GDP.*增长|国民经济",
    r"城市更新行动",
    r"央行.*降准|央行.*降息|货币政策",
    r"燃油|原油.*价格|石油.*出口",
    # 传统行业
    r"茅台|五粮液|白酒|金徽酒",
    r"永辉超市|开立医疗|网达软件",
]

# 投资信号关键词
STRONG_SIGNAL_KEYWORDS = [
    "ipo", "上市", "融资", "募资", "估值", "亿美元",
    "融资轮", "种子轮", "天使轮", "a轮", "b轮", "c轮",
    "基石", "超额认购",
]

WATCH_KEYWORDS = [
    "发布", "推出", "首发", "合作", "签约", "量产", "交付",
    "收购", "并购", "入股", "增持",
]

MAX_FINAL = 20  # 最终精选条数
FETCH_TIMEOUT = 8  # 单篇抓取超时秒数
FETCH_DELAY = 0.5  # 请求间隔秒数
MAX_SUMMARY_LEN = 500  # 摘要最大字数


def _smart_truncate(text: str, max_len: int) -> str:
    """智能截断：在自然断点处截断，避免生硬切断"""
    if not text or len(text) <= max_len:
        return text
    # 回退最多80字找自然断点（句号、逗号、换行等）
    for i in range(min(len(text), max_len), max_len - 80, -1):
        if i > 0 and text[i - 1] in '。，；！？.!?,;\n\r':
            return text[:i] + "..."
    # 找不到自然断点，直接截断
    return text[:max_len] + "..."


# ============================================================
# 正文抓取 + 规则摘要
# ============================================================

def fetch_article_content(url: str) -> Optional[str]:
    """抓取文章正文，返回纯文本；失败返回 None"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=FETCH_TIMEOUT)
        resp.encoding = "utf-8"

        if resp.status_code != 200:
            logger.warning(f"抓取失败 [{resp.status_code}]: {url}")
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # 财联社正文选择器
        detail = soup.select_one("div.detail-content")
        if not detail:
            # 备用：尝试常见正文容器
            for sel in ["article", ".article-content", ".news-content",
                         "[class*=detail-content]", "[class*=article-body]"]:
                detail = soup.select_one(sel)
                if detail and len(detail.get_text(strip=True)) > 200:
                    break

        if not detail:
            logger.warning(f"未找到正文区域: {url}")
            return None

        # 清理正文：去掉脚本/样式/广告等
        for tag in detail.select("script, style, iframe, .ad, .share, .related"):
            tag.decompose()

        text = detail.get_text(separator="\n", strip=True)
        # 去掉多余空行
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        if len(text) < 50:
            return None

        return text

    except requests.Timeout:
        logger.warning(f"抓取超时: {url}")
        return None
    except Exception as e:
        logger.warning(f"抓取异常: {url} - {str(e)}")
        return None


def generate_rule_summary(full_text: str, title: str) -> str:
    """从正文提取规则摘要，控制在 MAX_SUMMARY_LEN 字以内

    策略：逐行清理噪音 → 导语句子 + 含关键数据的句子，自然拼接。
    """
    if not full_text:
        return ""

    # ---- 第一步：逐行清理噪音（而非按段落整体丢弃）----
    # 财联社正文通常用 \n 分行，但行间无空行，不能依赖空行分段落

    raw_lines = [l.strip() for l in full_text.split("\n") if l.strip()]

    NOISE_CONTAINS = {
        "面对每天上千份", "请看财联社公司新闻部", "栏目，我们派驻全国",
        "公告里一堆专业术语", "重大事项公告动辄几十页几百页",
        "重点是啥？",
    }
    NOISE_STARTSWITH = (
        "编辑 牛占林", "编辑 周子意", "编辑 史正丞",
        "编辑 冯轶", "编辑 刘蕊", "编辑 夏军雄",
        "编辑 赵昊", "编辑 宋子乔", "编辑 王碧微",
        "财联社记者", "科创板日报记者", "蓝鲸科技记者",
        "小鲸注：",
    )
    DATE_LINE_RE = re.compile(
        r"^财联社\d{4}年\d{1,2}月\d{1,2}日讯(\s*（[编辑记者]\s*\S+?)?\s*）?$"
    )
    SHORT_LABEL_RE = re.compile(r"^《[^》]+》$|^【[^】]+$")

    def is_noise(line):
        if DATE_LINE_RE.match(line):
            return True
        if SHORT_LABEL_RE.match(line) and len(line) < 12:
            return True
        for pat in NOISE_CONTAINS:
            if pat in line:
                return True
        if line.startswith(NOISE_STARTSWITH):
            return True
        return False

    clean_lines = [line for line in raw_lines if not is_noise(line)]
    clean_text = "".join(clean_lines)

    if len(clean_text) < 30:
        return ""

    # ---- 第二步：切句 + 去重 ----
    sentences = re.split(r"(?<=[。！？；])", clean_text)
    sentences = [s.strip() for s in sentences if len(s.strip()) >= 10]

    # 数据关键词模式
    data_patterns = re.compile(
        r"\d+(?:\.?\d*)\s*(?:亿元|万元|亿美元|万美元|%|倍|万|亿|年|月|日"
        r"|跌|涨|增|减|占|超|达|约|近|同比|环比|估值|营收|净利|市值|融资|份额"
        r"|GB|TB|PB|nm|TWh)"
    )

    candidates = []
    seen = set()

    for sent in sentences:
        key = sent[:50]
        if key in seen:
            continue
        seen.add(key)
        candidates.append(sent)

    # ---- 第三步：拼接并截断 ----
    summary_parts = []
    total_len = 0
    limit = MAX_SUMMARY_LEN

    for sent in candidates:
        if total_len + len(sent) > limit + 60 and total_len > limit * 0.5:
            break
        summary_parts.append(sent)
        total_len += len(sent)

    result = "".join(summary_parts)

    # 硬截断到句子边界
    if len(result) > limit:
        result = result[:limit]
        last_punct = max(result.rfind("。"), result.rfind("！"), result.rfind("；"))
        if last_punct > limit * 0.6:
            result = result[:last_punct + 1]

    return result.strip()


def condense_rss_summary(text: str, max_chars: int = 300) -> str:
    """对 RSS 原始摘要做抽取式压缩，而非简单截断。

    策略：取第一句（导语），截断到150字以内。
    """
    if not text or len(text) <= 150:
        return text

    # 切句
    sentences = re.split(r"(?<=[。！？])", text)
    sentences = [s.strip() for s in sentences if len(s.strip()) >= 8]

    if not sentences:
        return _smart_truncate(text, max_chars)

    # 取第一句（导语句，通常是核心观点）
    first = sentences[0]

    # 如果第一句超过150字，截断到句子边界
    if len(first) > 150:
        return _smart_truncate(first, 150)

    return first


def enrich_summaries(news_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """批量抓取正文并生成规则摘要，失败则保留原摘要"""
    total = len(news_list)
    success = 0

    for i, news in enumerate(news_list, 1):
        url = news.get("link", "")
        if not url:
            continue

        logger.info(f"抓取正文 [{i}/{total}]: {news['title'][:30]}...")
        full_text = fetch_article_content(url)

        if full_text:
            summary = generate_rule_summary(full_text, news["title"])
            if summary and len(summary) >= 30:
                news["summary"] = summary
                news["has_full_summary"] = True
                success += 1
                logger.info(f"  ✓ 摘要已更新 ({len(summary)}字)")
            else:
                logger.info(f"  - 提取摘要过短({len(summary) if summary else 0}字)，保留原摘要")
        else:
            logger.info(f"  ✗ 正文抓取失败，保留原摘要")

        # 间隔防封
        time.sleep(FETCH_DELAY)

    logger.info(f"正文摘要增强完成: {success}/{total} 篇成功")
    return news_list


# ============================================================
# RSS 采集
# ============================================================

def collect_rss() -> List[Dict[str, Any]]:
    """采集所有财经RSS源（带自动重试）"""
    import time as _time
    all_news = []
    max_retries = 2  # 最多重试2次
    retry_delay = 30  # 重试间隔秒数

    for source in FINANCE_SOURCES:
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"采集: {source['name']} (第{attempt}次)")
                feed = feedparser.parse(source["url"], request_headers={
                    "User-Agent": "Mozilla/5.0 (compatible; FinanceBot/1.0)"
                })

                if feed.bozo and not feed.entries:
                    if attempt < max_retries:
                        logger.warning(f"RSS解析失败 [{source['name']}]: {feed.bozo_exception}，{retry_delay}秒后重试...")
                        _time.sleep(retry_delay)
                        continue
                    else:
                        logger.warning(f"RSS解析失败 [{source['name']}]: {feed.bozo_exception}，已达最大重试次数")
                        break

                # 采集成功，提取条目
                for entry in feed.entries[:source["max_items"]]:
                    title = entry.get("title", "").strip()
                    link = entry.get("link", "")
                    summary = _clean_html(entry.get("summary", entry.get("description", "")))
                    pub_date = entry.get("published", entry.get("updated", ""))

                    all_news.append({
                        "title": title,
                        "link": link,
                        "summary": _smart_truncate(summary, 500),
                        "published": pub_date,
                        "source": source["name"],
                        "type": "finance_rss",
                    })

                logger.info(f"从 {source['name']} 采集到 {len(feed.entries[:source['max_items']])} 条")
                break  # 采集成功，跳出重试循环

            except Exception as e:
                if attempt < max_retries:
                    logger.error(f"采集 {source['name']} 异常: {str(e)}，{retry_delay}秒后重试...")
                    _time.sleep(retry_delay)
                    continue
                else:
                    logger.error(f"采集 {source['name']} 失败: {str(e)}，已达最大重试次数")

    logger.info(f"财经RSS采集完成, 总计: {len(all_news)} 条")
    return all_news


def _clean_html(text: str) -> str:
    """清理HTML标签"""
    if not text:
        return ""
    try:
        soup = BeautifulSoup(str(text), "html.parser")
        return soup.get_text(separator=" ", strip=True)
    except Exception:
        return text


# ============================================================
# 华尔街见闻直接 API 采集（绕过 RSSHub）
# ============================================================

def collect_wallstreetcn() -> List[Dict[str, Any]]:
    """直接调华尔街见闻 API，返回与 feedparser 兼容的 dict 格式"""
    import time as _time

    url = "https://api-one.wallstcn.com/apiv1/content/lives"
    params = {"channel": "global-channel", "limit": 50, "first_page": True}

    for attempt in range(1, 3):
        try:
            logger.info(f"采集: 华尔街见闻 API (第{attempt}次)")
            resp = requests.get(url, params=params, headers={
                "User-Agent": "Mozilla/5.0 (compatible; FinanceBot/1.0)"
            }, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") != 20000:
                raise ValueError(f"API返回错误: {data.get('message', 'unknown')}")

            items = data.get("data", {}).get("items", [])
            news_list = []
            for item in items:
                title = item.get("title", "").strip()
                if not title:
                    continue
                link = (item.get("article") or {}).get("url", "") or item.get("uri", "")
                content = item.get("content_text", "") or item.get("content", "")
                summary = _smart_truncate(_clean_html(content), 500)
                pub_date = datetime.fromtimestamp(item["display_time"]).isoformat() if item.get("display_time") else ""

                news_list.append({
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "published": pub_date,
                    "source": "华尔街见闻",
                    "type": "finance_api",
                })

            logger.info(f"从 华尔街见闻 API 采集到 {len(news_list)} 条")
            return news_list

        except Exception as e:
            if attempt < 2:
                logger.error(f"华尔街见闻 API 采集异常: {str(e)}，30秒后重试...")
                _time.sleep(30)
            else:
                logger.error(f"华尔街见闻 API 采集失败: {str(e)}，已达最大重试次数")

    return []


# ============================================================
# 过滤
# ============================================================

def filter_by_relevance(news_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """领域相关性过滤：只保留与科技产业相关的财经新闻"""

    result = []
    excluded = 0

    for news in news_list:
        title = news.get("title", "")
        summary = news.get("summary", "")
        content = f"{title} {summary}".lower()

        # 强排除检测
        is_excluded = False
        for pattern in EXCLUDE_PATTERNS:
            try:
                if re.search(pattern, title, re.IGNORECASE):
                    is_excluded = True
                    excluded += 1
                    logger.debug(f"排除: {title[:50]}... (匹配: {pattern})")
                    break
            except re.error:
                continue

        if is_excluded:
            continue

        # 核心科技关键词检测（必须命中至少一个）
        has_tech_kw = any(kw.lower() in content for kw in CORE_TECH_KEYWORDS)

        if has_tech_kw:
            result.append(news)
        else:
            excluded += 1
            logger.debug(f"排除(无科技关键词): {title[:50]}...")

    logger.info(f"领域过滤: {len(news_list)}条 -> {len(result)}条 (排除 {excluded} 条)")
    return result


def _extract_topic_keywords(title: str) -> set:
    """从标题中提取话题关键词，用于话题聚类。
    
    策略：中文部分只取2字bigram（减少噪音），英文/数字按空格分词（>=2字符）。
    过滤停用词。
    """
    stop_words = {
        "的", "了", "在", "是", "和", "与", "将", "已", "也", "要",
        "能", "会", "被", "把", "让", "对", "从", "到", "又", "更",
        "最", "为", "上", "下", "中", "里", "后", "前", "出", "有",
        "能否", "如何", "什么", "怎样", "哪些",
        "重磅", "突发", "独家", "最新", "今日",
        "迎来", "临近", "能否", "怎样", "哪些",
        "一则", "一则", "一文", "一文", "来了",
    }
    words = set()
    # 清理引号等
    clean = re.sub(r'[""「」\'\'《》\[\]【】]', ' ', title)
    
    segments = re.split(r'\s+', clean)
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        cn_parts = re.findall(r'[\u4e00-\u9fff]+', seg)
        en_parts = re.findall(r'[a-zA-Z0-9]+', seg)
        
        for w in en_parts:
            w = w.lower()
            if len(w) >= 2 and w not in stop_words:
                words.add(w)
        
        # 中文只取2字bigram，减少噪音
        for cn in cn_parts:
            for i in range(len(cn) - 1):
                gram = cn[i:i+2]
                if gram not in stop_words:
                    words.add(gram)
    
    return words


def _topic_dedup(news_list: List[Dict[str, Any]], threshold: float = 0.15) -> List[Dict[str, Any]]:
    """基于标题关键词重叠度的话题聚类去重（零Token）
    
    原理：两篇标题如果关键词重叠度超过阈值，视为同一话题，
    只保留评分最高的那篇。
    使用 containment coefficient（包含系数）而非 Jaccard，
    因为 Jaccard 对集合大小敏感，会稀释共享关键词的信号。
    """
    if len(news_list) <= 1:
        return news_list
    
    keyword_sets = [_extract_topic_keywords(n["title"]) for n in news_list]
    
    n = len(news_list)
    group = list(range(n))
    
    def find(x):
        while group[x] != x:
            group[x] = group[group[x]]
            x = group[x]
        return x
    
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            group[rb] = ra
    
    for i in range(n):
        if not keyword_sets[i]:
            continue
        for j in range(i + 1, n):
            if not keyword_sets[j]:
                continue
            inter = len(keyword_sets[i] & keyword_sets[j])
            # containment coefficient: 交集 / 较小集合大小
            smaller = min(len(keyword_sets[i]), len(keyword_sets[j]))
            sim = inter / smaller if smaller > 0 else 0
            if sim >= threshold:
                union(i, j)
    
    from collections import defaultdict
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    
    result = []
    merged_count = 0
    for indices in groups.values():
        best = max(indices, key=lambda idx: news_list[idx].get("score", 0))
        result.append(news_list[best])
        merged_count += len(indices) - 1
    
    if merged_count > 0:
        merged_details = []
        for indices in groups.values():
            if len(indices) > 1:
                titles = [news_list[idx]["title"][:30] for idx in indices]
                merged_details.append(f"  → {titles}")
        logger.info(f"话题去重: 合并 {merged_count} 篇同类报道, {n}条 -> {len(result)}条")
        for detail in merged_details:
            logger.info(detail)
    return result


def deduplicate(news_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """基于标题精确去重"""
    seen = set()
    result = []
    for news in news_list:
        title_hash = hashlib.md5(news["title"].encode("utf-8")).hexdigest()[:12]
        if title_hash not in seen:
            seen.add(title_hash)
            result.append(news)
    logger.info(f"去重: {len(news_list)}条 -> {len(result)}条")
    return result


# ============================================================
# 评分 + 投资信号（v2：Phase 1 增强）
# 升级内容：
#   1. 事件类型粗分类（5档权重，替代原有布尔命中）
#   2. 金额规模因子（提取亿/万/%数字，动态加权）
#   3. 时效衰减（超12小时线性衰减）
# 参考：Janus-Q事件驱动框架 / 新闻学五要素
# ============================================================

from datetime import datetime, timezone, timedelta
from dateutil import parser as date_parser
from zoneinfo import ZoneInfo


def _parse_news_time(news: Dict[str, Any]) -> Optional[datetime]:
    """解析新闻发布时间，返回datetime；失败返回None"""
    pub_str = news.get("published", "")
    if not pub_str:
        return None
    # 常见RSS时间格式尝试
    for fmt in [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S%z",
        "%a, %d %b %Y %H:%M:%S %z",
    ]:
        try:
            return datetime.strptime(pub_str.strip(), fmt)
        except ValueError:
            continue
    # 兜底：用dateutil宽松解析
    try:
        dt = date_parser.parse(pub_str)
        # 如果解析出的是naive datetime，假设为北京时间
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return dt
    except Exception:
        return None


def _time_decay_factor(pub_dt: Optional[datetime]) -> float:
    """时效衰减系数（基于新闻学五要素·时新性）

    | 年龄      | 系数 | 说明               |
    |----------|------|--------------------|
    | < 6h     | 1.00 | 新鲜热点，不衰减    |
    | 6~12h    | 0.95 | 轻微衰减           |
    | 12~24h   | 0.85 | 明显衰减           |
    | > 24h    | 0.70 | 强烈衰减，接近旧闻 |
    """
    if not pub_dt:
        return 0.90  # 无法解析时给个保守默认值
    try:
        now = datetime.now(pub_dt.tzinfo) if pub_dt.tzinfo else datetime.now()
        # 处理时区差异，统一到当前时区比较
        if pub_dt.tzinfo and not now.tzinfo:
            now = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(pub_dt.tzinfo)
        elif now.tzinfo and not pub_dt.tzinfo:
            pub_dt = pub_dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(now.tzinfo)

        age_hours = (now - pub_dt).total_seconds() / 3600
        if age_hours < 0:
            return 1.00  # 未来时间（时区误差），不惩罚
        elif age_hours < 6:
            return 1.00
        elif age_hours < 12:
            return 0.95
        elif age_hours < 24:
            return 0.85
        else:
            return max(0.70, 1.0 - age_hours / 120)  # 超过5天最低0.7
    except Exception:
        return 0.90


def _extract_magnitude(content: str) -> float:
    """金额规模因子（参考Janus-Q·幅度塑形奖励）

    从标题+摘要中提取含财务单位的数字，返回乘数。
    大金额/具体数据 → 更可能是实质性事件 → 高分加成。

    | 提取到的规模       | 乘数  | 示例                     |
    |-------------------|-------|-------------------------|
    | ≥100亿 或 ≥10亿美元 | 1.30  | "融资200亿"              |
    | ≥10亿  或 ≥1亿美元  | 1.15  | "估值50亿"               |
| | 有具体百分比数据     | 1.10  | "增长23%"、"占比15%"      |
| | 有一般数据但无单位   | 1.05  | 含纯数字但无亿/万/%      |
    | 无任何数字          | 1.00  | 纯文字叙述               |
    """
    text = content.lower()

    # 百亿级：>=100亿 或 >=10亿美元
    if re.search(r'\d+(?:\.?\d*)\s*[万亿]?[亿美元]*.*?(?:100\s*亿|[1-9]\d{2}\s*亿)', text) or \
       re.search(r'(?:100\s*亿|[1-9]\d{2,}\s*亿|\d{2,}\s*亿美元)', text):
        return 1.30

    # 十亿级：>=10亿 或 >=1亿美元
    if re.search(r'(?:[1-9]\d\s*亿|[1-9]\d\d?\s*亿|\d+\s*亿美元)', text) or \
       re.search(r'[1-9]\d*\s*(?:亿|亿美元)', text):
        return 1.15

    # 有百分比数据
    if re.search(r'\d+(?:\.\d+)?\s*%', text):
        return 1.10

    # 有一般数字
    if re.search(r'\d{2,}', text):
        return 1.05

    return 1.00


# ---- 事件类型定义（参考Janus-Q 10类事件体系）----

EVENT_TYPES = {
    # Tier-A: 高影响资本事件（对应Janus-Q: Financing + Risk Warning）
    "tier_a_ipo": {
        "keywords": ["ipo", "上市", "港股上市", "科创板上市", "纳斯达克", "纽交所"],
        "label": "IPO/上市",
        "weight": 25,
        "icon": "🔴",
    },
    "tier_a_mega_deal": {
        "keywords": ["并购", "收购", "重组", "私有化", "要约"],
        "label": "并购重组",
        "weight": 24,
        "icon": "🔴",
    },
    # Tier-B: 重要融资动作
    "tier_b_finance": {
        "keywords": ["融资", "募资", "估值", "亿美元", "融资轮", "种子轮", "天使轮", "a轮", "b轮", "c轮", "基石", "超额认购"],
        "label": "融资估值",
        "weight": 20,
        "icon": "🟠",
    },
    # Tier-C: 业务里程碑
    "tier_c_milestone": {
        "keywords": ["首发", "量产", "交付", "中标", "落地", "获批", "认证"],
        "label": "业务里程碑",
        "weight": 14,
        "icon": "🟡",
    },
    "tier_c_collab": {
        "keywords": ["合作", "签约", "战略", "入股", "增持", "投资", "设立基金"],
        "label": "合作战略",
        "weight": 12,
        "icon": "🟡",
    },
    "tier_c_launch": {
        "keywords": ["发布", "推出", "上线", "开源", "首发", "揭幕"],
        "label": "产品发布",
        "weight": 11,
        "icon": "🟢",
    },
    # Tier-D: 财报与经营数据
    "tier_d_financials": {
        "keywords": ["财报", "净利", "净利润", "营收", "营业收入", "收入", "业绩预告", "盈利", "亏损"],
        "label": "财报业绩",
        "weight": 9,
        "icon": "📊",
    },
    "tier_d_market": {
        "keywords": ["股价", "市值", "涨停", "跌停", "大涨", "大跌", "回购", "分红", "派息", "股息"],
        "label": "市场表现",
        "weight": 8,
        "icon": "📊",
    },
}

# 核心公司分档（参考显著性要素）
TIER1_COMPANIES = ["英伟达", "nvidia", "openai", "anthropic", "台积电", "华为"]  # AI算力核心
TIER2_COMPANIES = ["宁德时代", "特斯拉", "腾讯", "阿里", "字节", "百度",
                   "meta", "google", "微软", "苹果", "亚马逊", "deepseek",
                   "智谱", "月之暗面", "kimi"]  # 科技巨头


def _classify_event(content: str, title: str) -> Tuple[Optional[Dict], int]:
    """事件类型分类 + 返回基础加分

    返回: (event_type_info或None, base_score_bonus)
    按优先级匹配（高权重类型优先），命中即停。
    """
    for event_id, info in EVENT_TYPES.items():
        for kw in info["keywords"]:
            if kw.lower() in content:
                return info, info["weight"]
    return None, 0


def score_and_classify(news_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """计算评分并识别投资信号（v2增强版）

    评分公式：
        最终分 = (基础分10 + 事件类型分 + 公司加分 + 创新加分)
                 × 金额规模因子 × 时效衰减系数
    """

    now = datetime.now()

    for news in news_list:
        title = news["title"]
        summary = news.get("summary", "")
        content = f"{title} {summary}".lower()

        # ── 基础分 ──
        score = 10

        # ── ① 事件类型分类（替代原有的布尔信号检测）──
        event_info, event_score = _classify_event(content, title)
        score += event_score

        # 映射回旧的signal_type（兼容报告模板）
        if event_info:
            weight = event_info["weight"]
            if weight >= 20:
                signal_type = "strong_signal"
            elif weight >= 10:
                signal_type = "watch"
            else:
                signal_type = "data_ref"
            event_label = event_info["label"]
            signal_hits = [f"{event_info['icon']} {event_label}"]
        else:
            signal_type = "data_ref"
            event_label = ""
            signal_hits = []

        # ── ② 核心公司加分（两档，显著性要素）──
        company_bonus = 0
        for company in TIER1_COMPANIES:
            if company.lower() in content:
                company_bonus = 8
                signal_hits.append(f"★{company}")
                break
        if company_bonus == 0:
            for company in TIER2_COMPANIES:
                if company.lower() in content:
                    company_bonus = 5
                    signal_hits.append(f"☆{company}")
                    break
        score += company_bonus

        # ── ③ 创新关键词加分 ──
        innovation_kw = ["首创", "突破", "全球第一", "sota", "首个", "刷新纪录", "世界首例", "行业首创"]
        innovation_hit = None
        for kw in innovation_kw:
            if kw in title:
                innovation_hit = kw
                score += 8
                break
        if innovation_hit:
            signal_hits.append(f"💡{innovation_hit}")

        # ── ④ 金额规模因子 ──
        mag_factor = _extract_magnitude(content)

        # ── ⑤ 时效衰减 ──
        pub_dt = _parse_news_time(news)
        decay_factor = _time_decay_factor(pub_dt)

        # ── 计算最终分数 ──
        raw_score = score
        final_score = round(raw_score * mag_factor * decay_factor, 1)

        news["score"] = final_score
        news["raw_score"] = raw_score  # 保留原始分用于调试对比
        news["signal_type"] = signal_type
        news["signal_hits"] = signal_hits
        news["event_type"] = event_label or "-"
        news["mag_factor"] = round(mag_factor, 2)
        news["decay_factor"] = round(decay_factor, 2)

        # 价值等级（阈值微调以适配新分数分布）
        if final_score >= 42:
            news["value_tier"] = "🔴 头条"
        elif final_score >= 32:
            news["value_tier"] = "🟠 重要"
        elif final_score >= 22:
            news["value_tier"] = "🟡 关注"
        else:
            news["value_tier"] = "⚪ 常规"

    # 按评分排序
    news_list.sort(key=lambda x: x["score"], reverse=True)
    return news_list[:MAX_FINAL]


# ============================================================
# 报告生成
# ============================================================

SIGNAL_ICONS = {
    "strong_signal": "🔥",
    "watch": "👀",
    "data_ref": "📊",
}

SIGNAL_LABELS = {
    "strong_signal": "强烈信号",
    "watch": "跟踪观察",
    "data_ref": "数据参考",
}

VALUE_ORDER = {"🔴 头条": 0, "🟠 重要": 1, "🟡 关注": 2, "⚪ 常规": 3}


def generate_report(news_list: List[Dict[str, Any]]) -> str:
    """生成Markdown格式财经日报"""

    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 统计
    total = len(news_list)
    value_counts = {}
    signal_counts = {"strong_signal": 0, "watch": 0, "data_ref": 0}
    for n in news_list:
        vt = n["value_tier"]
        value_counts[vt] = value_counts.get(vt, 0) + 1
        st = n["signal_type"]
        signal_counts[st] = signal_counts.get(st, 0) + 1

    lines = []
    lines.append("# 💰 财经科技资讯日报")
    lines.append("")
    lines.append(f"**日期**: {today}  ")
    lines.append(f"**生成时间**: {now}  ")
    lines.append(f"**新闻数量**: {total} 条")
    lines.append("")

    # 数据概览
    lines.append("## 📊 今日概览")
    lines.append("")
    value_str = " | ".join(f"**{k}** {value_counts.get(k, 0)}条" for k in ["🔴 头条", "🟠 重要", "🟡 关注", "⚪ 常规"] if value_counts.get(k, 0) > 0)
    lines.append(f"**价值分布**: {value_str}")
    lines.append("")
    signal_str = " | ".join(f"{SIGNAL_ICONS[k]} {SIGNAL_LABELS[k]} {v}条" for k, v in signal_counts.items() if v > 0)
    lines.append(f"**投资信号**: {signal_str}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Top5 高价值事件速览
    top5 = sorted(news_list, key=lambda x: (VALUE_ORDER.get(x["value_tier"], 99), -x["score"]))[:5]
    if top5:
        lines.append("## 🎯 今日高价值事件 Top 5")
        lines.append("")
        lines.append("| # | 事件 | 信号 |")
        lines.append("|---|------|------|")
        for i, n in enumerate(top5, 1):
            icon = SIGNAL_ICONS[n["signal_type"]]
            # 标题截短，避免表格过宽
            short_title = n["title"][:55] + "…" if len(n["title"]) > 55 else n["title"]
            short_title = short_title.replace("|", "｜")
            lines.append(f"| {i} | **{short_title}** | {n['value_tier']} {icon} {SIGNAL_LABELS[n['signal_type']]} |")
        lines.append("")
        lines.append("---")
        lines.append("")

    # 投资信号雷达（表格形式）

    signal_news = [n for n in news_list if n["signal_type"] != "data_ref"]
    if signal_news:
        lines.append("## 💰 投资信号雷达")
        lines.append("")
        lines.append("> 识别潜在投资参考方向")
        lines.append("")
        lines.append("| 信号 | 标题 | 价值 | 来源 |")
        lines.append("|------|------|------|------|")
        for n in signal_news:
            icon = SIGNAL_ICONS[n["signal_type"]]
            label = SIGNAL_LABELS[n["signal_type"]]
            short_title = n["title"][:55] + "…" if len(n["title"]) > 55 else n["title"]
            # 修复：标题中的 | 会破坏Markdown表格列分隔，替换为全角｜
            short_title = short_title.replace("|", "｜")
            lines.append(f"| {icon} {label} | {short_title} | {n['value_tier']} | {n['source']} |")
        lines.append("")
        lines.append("---")
        lines.append("")

    # Our Take — 核心投资观点（v2.0 Anthropic方法论）
    top_items = [n for n in news_list if n["value_tier"] in ("🔴 头条", "🟠 重要")][:3]
    if top_items:
        lines.append("## 💡 Our Take")
        lines.append("")
        lines.append("> *Opinionated Take — 不只告诉发生了什么，更说这意味着什么*")
        lines.append("")

        # 针对头条事件生成观点
        for j, item in enumerate(top_items, 1):
            event = item.get("event_type", "重大事件")
            score = item.get("score", 0)
            signal = item.get("signal_type", "")
            signal_hits = item.get("signal_hits", [])

            # 基于事件类型生成观点模板
            take_templates = {
                "IPO/上市": "资本市场窗口期值得关注，重点看发行估值、基石投资者和首日表现",
                "并购/重组": "行业整合信号，关注交易对价合理性、协同效应和监管审批风险",
                "融资/估值": "资本在向该赛道集中，关注融资金额、投后估值和后续轮次节奏",
                "业务里程碑": "商业化进展是关键验证节点，关注后续订单和收入转化",
                "合作/战略": "产业链合作深化，关注合作深度（独家/非独家）和实际落地节奏",
                "产品发布": "产品竞争力取决于实际参数和客户反馈，关注早期adoption数据",
                "业绩/财报": "核心看营收增速和利润率的边际变化，关注管理层指引调整",
                "市场表现": "短期波动 vs 长期趋势，关注基本面是否有实质性变化",
            }
            take = take_templates.get(event, "持续跟踪该事件后续发展，关注其对产业链的影响")

            if signal == "strong_signal":
                take = "🔥 强烈信号：" + take

            short_title = item["title"][:60] + "…" if len(item["title"]) > 60 else item["title"]
            lines.append(f"**{j}.** *{event}* — {short_title}")
            lines.append(f"   **Our Take**: {take}")
            if signal_hits:
                keywords = [h for h in signal_hits if not h.startswith("★")]
                if keywords:
                    lines.append(f"   关键词: {' · '.join(keywords[:4])}")
            lines.append("")

        # 今日信号总结
        strong_count = sum(1 for n in news_list if n["signal_type"] == "strong_signal")
        ipo_count = sum(1 for n in news_list if "IPO" in str(n.get("event_type", "")))
        if ipo_count >= 2:
            lines.append(f"> ⚡ **IPO 热潮**: 今日 {ipo_count} 条 IPO 相关动态，关注港股/A股 IPO 窗口")
            lines.append("")
        elif strong_count >= 2:
            lines.append(f"> ⚡ **信号密度高**: 今日 {strong_count} 条强烈信号，建议重点关注")
            lines.append("")

        lines.append("---")
        lines.append("")

    # Thesis Tracker — 持续追踪标记（v2.0 Anthropic方法论）
    thesis_candidates = []
    for n in news_list:
        if n["signal_type"] == "strong_signal" and n["value_tier"] in ("🔴 头条", "🟠 重要"):
            event = n.get("event_type", "")
            if event in ("IPO/上市", "并购/重组", "融资/估值"):
                thesis_candidates.append(n)

    if thesis_candidates:
        lines.append("## 📌 Thesis Tracker")
        lines.append("")
        lines.append("> *持续追踪的核心投资命题 — 这些事件值得标记，后续验证*")
        lines.append("")
        lines.append("| 命题 | 验证节点 | 当前状态 |")
        lines.append("|------|----------|----------|")
        for n in thesis_candidates[:5]:
            short_title = n["title"][:45] + "…" if len(n["title"]) > 45 else n["title"]
            short_title = short_title.replace("|", "｜")
            event = n.get("event_type", "跟踪中")
            # 生成验证节点描述
            verify_map = {
                "IPO/上市": "关注首日表现和估值",
                "并购/重组": "关注交易进展和监管审批",
                "融资/估值": "关注后续轮次和估值变化",
            }
            verify = verify_map.get(event, "等待后续进展")
            lines.append(f"| {short_title} | {verify} | 🔴 待验证 |")
        lines.append("")
        lines.append(f"> 💡 建议：将以上事件加入自选跟踪，在下周日报中回溯验证")
        lines.append("")
        lines.append("---")
        lines.append("")

    # 全部资讯列表
    lines.append("## 📋 资讯列表")
    lines.append("")
    for i, n in enumerate(news_list, 1):
        icon = SIGNAL_ICONS[n["signal_type"]]
        lines.append(f"**{i}. {n['title']}**  {n['value_tier']} | {icon} {SIGNAL_LABELS[n['signal_type']]} | 来源: {n['source']}")
        if n.get("summary"):
            summary_text = condense_rss_summary(n["summary"], MAX_SUMMARY_LEN)
            lines.append(f"> {summary_text}")
        lines.append(f"🔗 [{n['link']}]({n['link']})")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*本报告由财经资讯采集系统自动生成，聚焦科技相关财经事件*")

    return "\n".join(lines)


def _extract_one_liner(news: Dict[str, Any]) -> str:
    """从摘要提取一句话≤80字"""
    summary = news.get("summary", news.get("generated_summary", ""))
    if not summary:
        return ""
    # 取第一句话
    sentences = re.split(r'[。！？\n]', summary)
    for s in sentences:
        s = s.strip()
        if len(s) >= 15 and len(s) <= 100:
            return s[:80]
    return summary[:80]


# ============================================================
# 主流程
# ============================================================

def main():
    """主流程"""
    logger.info("=" * 60)
    logger.info("财经资讯独立采集系统")
    logger.info("=" * 60)

    today = datetime.now().strftime("%Y-%m-%d")
    report_filename = f"finance_news_{today}.md"
    json_filename = f"finance_news_{today}.json"
    report_path = os.path.join(REPORTS_DIR, report_filename)
    json_path = os.path.join(REPORTS_DIR, json_filename)

    # 1. 采集财经RSS源（需要本地 RSSHub: localhost:1200）
    logger.info("步骤 1/6: 采集财经RSS源")
    all_news = collect_rss()
    if not all_news:
        logger.warning("未采集到任何新闻，退出")
        return

    # 2. 去重 + 领域过滤
    logger.info("步骤 2/6: 去重 + 领域过滤")
    deduped = deduplicate(all_news)
    filtered = filter_by_relevance(deduped)

    # 3. 评分 + 投资信号
    logger.info("步骤 3/6: 评分 + 投资信号识别")
    scored = score_and_classify(filtered)
    logger.info(f"评分完成: {len(scored)} 条")

    # 3.5 话题聚类去重（同话题不同标题的报道只保留评分最高的）
    scored = _topic_dedup(scored)
    logger.info(f"最终精选: {len(scored)} 条")

    # 4. 抓取正文 + 生成规则摘要（替代RSS截断摘要）
    logger.info("步骤 4/6: 抓取正文 + 生成规则摘要")
    scored = enrich_summaries(scored)

    # 5. 生成报告
    logger.info("步骤 5/6: 生成报告")
    report = generate_report(scored)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    logger.success(f"报告已生成: {report_path}")

    # JSON数据
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(scored, f, ensure_ascii=False, indent=2)

    # 复制到Obsidian（需设置 OBSIDIAN_DIR 环境变量）
    if OBSIDIAN_DIR and os.path.exists(OBSIDIAN_DIR):
        obsidian_path = os.path.join(OBSIDIAN_DIR, report_filename)
        shutil.copy2(report_path, obsidian_path)
        logger.success(f"已同步到Obsidian: {obsidian_path}")

    logger.success("财经资讯采集完成!")
    print(f"\n📄 请查看报告: {report_path}")
    print(f"📊 共精选 {len(scored)} 条财经科技资讯")


if __name__ == "__main__":
    main()

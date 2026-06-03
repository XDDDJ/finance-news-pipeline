---
name: investment-news-collector
description: 零Token投资资讯采集系统。从财联社RSS自动抓取，经6步管线（去重→领域过滤→事件评分→话题聚类→正文摘要→日报生成），输出结构化财经日报。纯Python规则引擎，无需API Key，开箱即用。
---

# investment-news-collector

> 🚀 **零Token、零API Key、零外部依赖**的财经资讯自动采集系统

## 亮点

- **零成本运行**：纯Python规则引擎，无需调用任何大模型API，无Token消耗
- **6步智能管线**：RSS采集 → MD5去重 → 76关键词领域过滤 → 8类型事件评分 → 话题聚类去重 → 正文自动摘要
- **投资信号识别**：自动标注🔥强烈/👀跟踪/📊数据三档信号，辅助投资决策
- **自包含单文件**：1011行Python，无内部模块依赖，复制即用
- **Obsidian同步**：日报自动生成Markdown格式，可一键同步到Obsidian知识库

## 架构总览

```
RSS源(财联社深度) → MD5去重 → 76关键词领域过滤
    ↓
8类型事件评分(含公司加分/创新加分/金额因子/时效衰减)
    ↓
话题聚类去重(Containment Coefficient) → 正文抓取+规则摘要(零Token)
    ↓
生成日报(Markdown) → 同步Obsidian(可选)
```

## 安装

### 1. Python 依赖

```bash
pip install feedparser beautifulsoup4 loguru requests
```

### 2. 本地 RSSHub

采集需要 RSSHub 将华尔街见闻 API 转为 RSS 格式。项目自带 `start_rsshub.py`。

**一次安装**：
```bash
# 下载 + 解压到 D:\RSSHub
curl -L -o rsshub.zip https://github.com/DIYgod/RSSHub/archive/refs/heads/master.zip
mkdir D:\RSSHub && unzip rsshub.zip -d D:\ && robocopy D:\RSSHub-master D:\RSSHub /E /MOV

# 安装依赖
cd D:\RSSHub
npm install --legacy-peer-deps --cache .npm-cache
```

### 3. 配置（可选）

```python
OBSIDIAN_DIR = os.environ.get("OBSIDIAN_DIR", "")  # 留空跳过 Obsidian 同步
```

### 4. 运行

```bash
# 1. 启动本地 RSSHub（开发模式，无需构建）
python start_rsshub.py
# 或手动: cd D:\RSSHub && node --import tsx lib/index.ts --port 1200

# 2. 采集（约2-3分钟）
python src/finance_main.py

# 3. 关闭 RSSHub（可选）
python start_rsshub.py stop
```

报告生成在 `reports/finance_news_YYYY-MM-DD.md`。

## 数据源

### 主源：华尔街见闻（当前可用）

通过本地 RSSHub 的 `wallstreetcn/news/global` 路由获取。该路由目前稳定。

### 备源：财联社深度（待修复）

CLS 新增 API 签名验证，RSSHub 路由返回 503。待 RSSHub 社区更新签名算法后恢复。

### ⚠️ 为什么需要 RSSHub？

华尔街见闻的 API 返回复杂 JSON，直接对接成本高。RSSHub 作为标准化层，将各网站 API 统一转为 RSS XML，与现有 feedparser 管线无缝衔接。

> 已保留 `collect_wallstreetcn()` 函数用于未来直连 API 方案，当前因响应体过大和字段映射问题暂不使用。

### 评分公式

```
最终分 = (10 + 事件类型分 + 公司加分 + 创新加分) × 金额规模因子 × 时效衰减
```

### 事件类型（8类，按优先级匹配）

| 类型 | 权重 | 说明 |
|------|------|------|
| IPO | 25 | 上市、港股上市、纳斯达克、纽交所 |
| 并购 | 24 | 并购、收购、重组、私有化 |
| 融资 | 20 | 融资、募资、A/B/C轮、基石、超额认购 |
| 里程碑 | 14 | 首发、量产、交付、中标、落地 |
| 合作 | 12 | 合作、签约、战略、入股、增持 |
| 发布 | 11 | 发布、推出、上线、开源 |
| 财报 | 9 | 财报、净利、营收、业绩预告 |
| 市场 | 8 | 股价、市值、涨停、回购 |

### 核心公司加分

- **Tier1（+8分）**：英伟达、OpenAI、Anthropic、台积电、华为
- **Tier2（+5分）**：特斯拉、腾讯、阿里、字节、Meta、Google、微软、苹果、DeepSeek、智谱等

### 投资信号映射

- 🔥 **强烈信号**：权重 ≥ 20（IPO/并购/融资）
- 👀 **跟踪观察**：权重 10-19（里程碑/合作/发布）
- 📊 **数据参考**：权重 < 10（财报/市场表现）

### 价值等级

- 🔴 头条：≥ 42 分
- 🟠 重要：≥ 32 分
- 🟡 关注：≥ 22 分
- ⚪ 常规：< 22 分

## 话题聚类去重

解决"同一事件多家报道"问题：
- **精确去重**：MD5哈希（标题+链接）
- **语义去重**：中文2字bigram + 英文token，Containment Coefficient（阈值0.15）
- 同组保留评分最高的一篇

## 正文摘要增强

零Token纯规则引擎，从财联社原文提取摘要：
1. CSS选择器提取正文（`div.detail-content`）
2. 逐行噪音过滤（栏目引导语、编辑署名、记者行等）
3. 导语句 + 数据句拼接（≤500字）
4. 抓取失败自动降级为RSS原始摘要
5. 智能截断：在自然断点处截断，避免生硬切断

## 输出格式

生成 Markdown 日报（v2.0 参考 Anthropic Financial Services 方法论优化），包含 7 个板块：

- 📊 今日概览 — 价值分布 + 投资信号统计
- 💡 Our Take — 核心投资观点与分析（Opinionated Take）
- 🎯 今日高价值事件 Top 5
- 📌 Thesis Tracker — 持续追踪的投资命题和催化剂
- 💰 投资信号雷达
- 📋 完整资讯列表（含摘要、标签、链接）

## 方法论来源

v2.0 起，日报输出理念参考 [Anthropic Financial Services](https://github.com/anthropics/financial-services) 框架：

- **Our Take（观点驱动）**：不只列事件，还要说"这意味着什么"——参考 Morning Note 的 Top Call 理念
- **Thesis Tracker（命题追踪）**：对重大融资/并购/IPO事件建立持续追踪标记——参考 Thesis Tracker 的 Pillars/Risks/Catalysts 框架
- **信号 vs 噪音**：明确区分 actionable 信号和背景信息

## 工程经验

- **RSS源选择**：主源 CLS Depth，备源华尔街见闻。数据源要有降级链
- **采集时间**：建议早8-9点后运行（凌晨RSS源偶发异常）
- **相似度算法**：关键词bigram场景用Containment Coefficient，别用Jaccard
- **噪音过滤粒度**：行级过滤 > 段落级过滤（财联社正文无空行分隔）
- **按需启停**：RSSHub 采集前启动、采集后关闭，不常驻
- **沙箱适配**：Windows 沙箱下 git clone 不可用，用 curl+zip 兜底；npm 缓存目录需设为本地路径

## 与 ai-news-collector 的分工

| 系统 | 覆盖范围 | 来源 |
|------|----------|------|
| ai-news-collector | AI/具身智能/芯片/新能源等技术新闻 | 36氪、量子位、InfoQ、钛媒体 |
| investment-news-collector | 港股IPO、上市暴涨、财报解读、融资并购 | 财联社深度 |

两者并行运行，互补覆盖科技产业的完整信息面。

import os
import re
import sys
import time
import json
import html as html_lib
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from google import genai
from google.genai import types

# =========================
# ⚙️ 設定區（從環境變數讀取）
# =========================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
FORCE_RERUN    = os.environ.get("FORCE_RERUN", "false").lower() == "true"
SITE_TITLE     = os.environ.get("SITE_TITLE", "股癌筆記")
SITE_BASEURL   = os.environ.get("SITE_BASEURL", "")  # GitHub Pages 子路徑可填，例如 "/gooaye-notes"

client  = genai.Client(api_key=GEMINI_API_KEY)
RSS_URL = "https://open.firstory.me/rss/user/ck7t2i2ncqopi0873tqaimwq1"

# 儲存路徑（會被 commit 回 GitHub）
OUTPUT_DIR        = "output"                            # 逐字稿 + 原始分析 JSON
SITE_DIR          = "site"                              # 對外發佈的靜態網站根目錄
SITE_EPISODES_DIR = os.path.join(SITE_DIR, "episodes")  # 每集 HTML
SITE_DATA_DIR     = os.path.join(SITE_DIR, "data")      # 集數索引 + 單集 JSON
HISTORY_FILE      = "last_ep.txt"

for d in (OUTPUT_DIR, SITE_DIR, SITE_EPISODES_DIR, SITE_DATA_DIR):
    os.makedirs(d, exist_ok=True)

UNRESTRICTED_SAFETY = [
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,      threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,        threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
]

# =========================
# 📡 模組 1：抓取最新 RSS
# =========================
def get_latest_episode_from_rss(rss_url):
    print("📡 正在抓取 RSS...")
    podcast_agents = [
        "Overcast/3.0 (+http://overcast.fm/; iOS podcast app)",
        "PocketCasts/7.0",
        "AppleCoreMedia/1.0.0.18G82 (iPhone; U; CPU iPhone OS 14_7 like Mac OS X)",
        "Castro/2022 (iOS 15.0)",
    ]
    encoded = requests.utils.quote(rss_url, safe='')
    sources = [
        {"name": "direct_overcast",    "url": rss_url,                                              "ua": podcast_agents[0]},
        {"name": "direct_pocketcasts", "url": rss_url,                                              "ua": podcast_agents[1]},
        {"name": "direct_apple",       "url": rss_url,                                              "ua": podcast_agents[2]},
        {"name": "corsproxy",          "url": f"https://corsproxy.io/?{encoded}",                   "ua": "Mozilla/5.0"},
        {"name": "codetabs",           "url": f"https://api.codetabs.com/v1/proxy?quest={encoded}", "ua": "Mozilla/5.0"},
        {"name": "allorigins_raw",     "url": f"https://api.allorigins.win/raw?url={encoded}",      "ua": "Mozilla/5.0"},
    ]

    xml_content = None
    for src in sources:
        try:
            print(f"   嘗試：{src['name']}...")
            r = requests.get(src["url"], timeout=20, headers={"User-Agent": src["ua"]}, allow_redirects=True)
            print(f"   狀態碼：{r.status_code}")
            if r.status_code != 200:
                continue
            if "<item>" in r.text or "<entry>" in r.text:
                print(f"   ✅ {src['name']} 成功")
                xml_content = r.text
                break
        except Exception as e:
            print(f"   {src['name']} 失敗：{e}")

    if not xml_content:
        raise Exception("❌ 所有方式均失敗")

    root         = ET.fromstring(xml_content)
    latest_item  = root.find('.//channel/item')
    title        = latest_item.find('title').text
    mp3_url      = latest_item.find('enclosure').attrib['url']
    pub_date_raw = latest_item.findtext('pubDate', default='')

    match      = re.search(r"(EP\d+)", title, re.IGNORECASE)
    episode_no = match.group(1).upper() if match else "LATEST_EP"
    print(f"✅ 集數：{episode_no}  |  發布：{pub_date_raw}")
    return episode_no, title, mp3_url, pub_date_raw

# =========================
# 📝 模組 2：音檔 → 逐字稿
# =========================
def get_full_transcript(mp3_path, episode_no):
    txt_path = os.path.join(OUTPUT_DIR, f"{episode_no}_transcript.txt")
    if os.path.exists(txt_path):
        print(f"⚡ 逐字稿快取命中：{txt_path}")
        with open(txt_path, "r", encoding="utf-8") as f:
            return f.read()

    print("☁️ 上傳音檔至 Google...")
    audio_file = client.files.upload(file=mp3_path)
    while audio_file.state.name == "PROCESSING":
        time.sleep(5)
        audio_file = client.files.get(name=audio_file.name)
    if audio_file.state.name == "FAILED":
        raise ValueError("❌ 音檔處理失敗")

    print("✍️ 產生逐字稿...")
    prompt = (
        "請根據這段音檔，整理出一份完整的繁體中文逐字稿。"
        "要求：盡可能一字不漏，加上標點符號與適當分段。"
        "請務必加上時間戳記，格式為「[分:秒] 文字」。"
        "不需做總結或摘要。"
    )
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[audio_file, prompt],
        config=types.GenerateContentConfig(safety_settings=UNRESTRICTED_SAFETY),
    )
    client.files.delete(name=audio_file.name)
    transcript = response.text.strip()

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(transcript)
    print(f"💾 逐字稿已儲存：{txt_path}")
    return transcript

# =========================
# 🧠 模組 3：逐字稿 → JSON 分析
# =========================

# JSON 欄位順序（匯出時依此排序，閱讀體驗一致）
META_KEYS    = ["episode", "date", "pub_date", "title", "intro"]
MARKET_KEYS  = ["sentiment", "summary"]
NEWS_KEYS    = ["title", "category", "event", "view"]
STOCK_KEYS   = ["name", "ticker", "market", "sentiment", "risk",
                "catalyst_short", "risk_note", "summary",
                "price", "rsi", "change_1m", "pe", "holding"]
QA_KEYS      = ["title", "question", "points", "quote"]
POINT_KEYS   = ["label", "content"]

def _ordered(d, keys):
    """依 keys 順序重建 dict，未列出的 key 放在最後。"""
    if not isinstance(d, dict):
        return d
    out = {}
    for k in keys:
        if k in d:
            out[k] = d[k]
    for k, v in d.items():
        if k not in out:
            out[k] = v
    return out

def normalize_payload(parsed, episode_no, pub_date_raw):
    """把 Gemini 回來的 JSON 整理成固定欄位順序、好讀的形狀。"""
    parsed = dict(parsed or {})
    parsed.setdefault("episode", episode_no)
    parsed.setdefault("pub_date", pub_date_raw or None)

    if isinstance(parsed.get("market_view"), dict):
        parsed["market_view"] = _ordered(parsed["market_view"], MARKET_KEYS)

    parsed["news"] = [_ordered(n, NEWS_KEYS) for n in parsed.get("news", []) or []]

    stocks = []
    for s in parsed.get("stocks", []) or []:
        s = _ordered(s, STOCK_KEYS)
        stocks.append(s)
    parsed["stocks"] = stocks

    qa_list = []
    for qa in parsed.get("qa", []) or []:
        qa = dict(qa)
        qa["points"] = [_ordered(p, POINT_KEYS) for p in qa.get("points", []) or []]
        qa_list.append(_ordered(qa, QA_KEYS))
    parsed["qa"] = qa_list

    return _ordered(parsed, META_KEYS + ["market_view", "news", "stocks", "qa"])

def dump_pretty_json(obj, path):
    """寫出排版整齊的 JSON：UTF-8 / indent=2 / 緊湊分隔符。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, separators=(",", ": "))
        f.write("\n")

def analyze_from_transcript(transcript_text, pub_date_raw, episode_no):
    json_path = os.path.join(OUTPUT_DIR, f"{episode_no}_analysis.json")
    if os.path.exists(json_path) and not FORCE_RERUN:
        print(f"⚡ 分析快取命中：{json_path}")
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)

    print("🧠 結構化分析中...")
    date_hint = f"此集 RSS 發布時間為：{pub_date_raw}，請據此推斷並填入 date 欄位（格式 YYYY-MM-DD）。" if pub_date_raw else ""

    prompt = f"""
你是專業的財經內容編輯，請根據以下逐字稿進行分析，用繁體中文輸出，只輸出純 JSON，不含任何說明文字或 markdown 符號。
{date_hint}
━━━━━━━━━━━━━━━━━━━━━━━━
【STEP 1｜內容過濾】
━━━━━━━━━━━━━━━━━━━━━━━━
逐字稿包含以下四段，請依規則處理：
① 業配廣告    → 完全忽略
② 日常閒聊    → 完全忽略
③ 股市市場話題 → 核心，完整分析
④ Q&A        → 只保留投資相關內容
━━━━━━━━━━━━━━━━━━━━━━━━
【STEP 2｜語氣與用字規範】
━━━━━━━━━━━━━━━━━━━━━━━━
✅ 應使用：客觀、第三人稱的財經媒體語氣（「分析師認為」「市場觀點」「據悉」「值得關注」）
❌ 絕對不能出現：主持人、來賓、節目、本集、集數、聽眾、podcast、節目中、本期、這集、上集、下集、時間戳記、分鐘數、來源標記
━━━━━━━━━━━━━━━━━━━━━━━━
【STEP 3｜欄位規則】
━━━━━━━━━━━━━━━━━━━━━━━━
■ date：YYYY-MM-DD，無法判斷填 null
■ title：20字內財經頭條
■ intro：150-200字市場摘要（市場背景→核心話題→投資議題概覽）
■ market_view：sentiment（看多/看空/中性）+ summary（20字內市場核心判斷）
■ news：每事件一張卡，title 8字內、category（台股/美股/半導體/總經/其他）、event 客觀2句、view 第三人稱2-3句
■ stocks：至少 3 檔，ticker 台股填數字、美股填英文（台積電=2330、聯發科=2454），market（台股半導體/美股半導體/台股網通/台股其他/美股其他），summary 200字內含(a)核心理由(b)短線觀察(c)風險，catalyst_short 10字內或 null，risk_note 15字內
■ qa：title 15字內聳動、question 1-2句精煉、points 3-4 個（label 4字內）、quote 20-40字精華金句
━━━━━━━━━━━━━━━━━━━━━━━━
【STEP 4｜Q&A 過濾標準】
━━━━━━━━━━━━━━━━━━━━━━━━
✅ 納入：選股持股賣股、個股產業看法、投資心法、倉位管理、槓桿維持率、總經判斷
❌ 跳過：純生活問題、與投資無關的個人意見
━━━━━━━━━━━━━━━━━━━━━━━━
【輸出 JSON 結構】
━━━━━━━━━━━━━━━━━━━━━━━━
{{
  "date": "YYYY-MM-DD 或 null",
  "title": "財經頭條20字以內",
  "intro": "150-200字市場摘要",
  "market_view": {{ "sentiment": "看多|看空|中性", "summary": "20字以內市場核心判斷" }},
  "news":   [{{ "title": "", "category": "", "event": "", "view": "" }}],
  "stocks": [{{ "name": "", "ticker": "", "market": "", "sentiment": "", "risk": "",
                "catalyst_short": "", "risk_note": "", "summary": "",
                "price": null, "rsi": null, "change_1m": null, "pe": null, "holding": null }}],
  "qa":     [{{ "title": "", "question": "",
                "points": [{{ "label": "", "content": "" }}],
                "quote": "" }}]
}}

【逐字稿】
{transcript_text}
"""
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2,
            safety_settings=UNRESTRICTED_SAFETY,
        ),
    )

    raw = response.text.strip()
    raw = re.sub(r"^```(?:json)?", "", raw, flags=re.IGNORECASE).strip()
    raw = re.sub(r"```$", "", raw).strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"❌ JSON 解析失敗：{e}")
        print("原始內容前 300 字：", raw[:300])
        raise

    parsed = normalize_payload(parsed, episode_no, pub_date_raw)
    dump_pretty_json(parsed, json_path)
    print(f"💾 分析結果已儲存：{json_path}")
    return parsed

# =========================
# 🌐 模組 4：JSON → 靜態網站
# =========================
def _esc(x):
    return html_lib.escape("" if x is None else str(x), quote=True)

def _format_pub_date(pub_date_raw):
    """RFC822 → YYYY-MM-DD（失敗就回原字串）。"""
    if not pub_date_raw:
        return ""
    try:
        dt = parsedate_to_datetime(pub_date_raw)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return pub_date_raw

def render_episode_page(data):
    """渲染單集 HTML 頁面，沿用原本卡片視覺設計。"""
    episode  = data.get("episode", "")
    date_str = data.get("date") or _format_pub_date(data.get("pub_date")) or "未知日期"
    title    = data.get("title") or "市場重點整理"

    parts = [f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(episode)} {_esc(title)} | {_esc(SITE_TITLE)}</title>
<link rel="stylesheet" href="../assets/style.css">
</head>
<body>
<header class="topbar">
  <a class="brand" href="../index.html">← {_esc(SITE_TITLE)}</a>
  <span class="ep-tag">{_esc(episode)}</span>
</header>

<main class="container">
  <div class="ep-header">
    <span class="date-pill">{_esc(date_str)}</span>
    <h1>{_esc(title)}</h1>
  </div>

  <section class="intro-box">
    <h4>💡 本週市場摘要</h4>
    <p>{_esc(data.get('intro',''))}</p>
  </section>
"""]

    # 大盤觀點
    market    = data.get("market_view") or {}
    sentiment = market.get("sentiment", "中性")
    s_class   = {"看多": "bull", "看空": "bear"}.get(sentiment, "neutral")
    parts.append(f"""
  <h3 class="section-title">📊 市場觀點</h3>
  <div class="market-row {s_class}">
    <span class="badge">● {_esc(sentiment)}</span>
    <span class="market-summary">{_esc(market.get('summary',''))}</span>
  </div>
""")

    # 市場事件
    if data.get("news"):
        parts.append('<h3 class="section-title">📰 市場事件與解析</h3>')
        for n in data["news"]:
            cat = n.get("category", "")
            cat_class = {"台股":"cat-tw","美股":"cat-us","半導體":"cat-semi","總經":"cat-macro"}.get(cat, "cat-other")
            parts.append(f"""
  <article class="card news">
    <div class="card-head">
      <b>📌 {_esc(n.get('title',''))}</b>
      <span class="badge {cat_class}">{_esc(cat)}</span>
    </div>
    <p><b class="muted">事件：</b>{_esc(n.get('event',''))}</p>
    <p><b>市場解讀：</b>{_esc(n.get('view',''))}</p>
  </article>""")

    # 個股觀點
    if data.get("stocks"):
        parts.append('<h3 class="section-title">📈 個股觀點</h3>')
        for s in data["stocks"]:
            sent  = s.get("sentiment", "觀望")
            sent_class = {"看多":"bull","看空":"bear"}.get(sent, "watch")
            risk  = s.get("risk", "")
            risk_class = {"高":"risk-high","中":"risk-mid","低":"risk-low"}.get(risk, "")
            ticker = f"({_esc(s.get('ticker'))})" if s.get("ticker") else ""
            parts.append(f"""
  <article class="card stock {sent_class}">
    <div class="card-head">
      <div>
        <b class="stock-name">{_esc(s.get('name',''))}</b>
        <span class="ticker">{ticker}</span>
        <span class="market-tag">{_esc(s.get('market',''))}</span>
      </div>
      <span class="badge {sent_class}">{_esc(sent)}</span>
    </div>
    <div class="chips">
      <span class="chip {risk_class}">⚠️ 風險：<b>{_esc(risk) or '—'}</b></span>
      <span class="chip">🚀 催化劑：{_esc(s.get('catalyst_short') or '—')}</span>
      <span class="chip">🛡️ 風險點：{_esc(s.get('risk_note') or '—')}</span>
    </div>
    <p class="stock-summary">{_esc(s.get('summary',''))}</p>
  </article>""")

    # 投資議題
    if data.get("qa"):
        parts.append('<h3 class="section-title">💼 投資議題探討</h3>')
        for qa in data["qa"]:
            pts = "".join(
                f'<p class="point">• <b>{_esc(p.get("label",""))}：</b>{_esc(p.get("content",""))}</p>'
                for p in qa.get("points", [])
            )
            parts.append(f"""
  <article class="card qa">
    <h4>💬 {_esc(qa.get('title',''))}</h4>
    <div class="qa-question"><p>{_esc(qa.get('question',''))}</p></div>
    {pts}
    <div class="qa-quote"><b>「{_esc(qa.get('quote',''))}」</b></div>
  </article>""")

    parts.append("""
  <p class="disclaimer">僅供參考，不構成投資建議</p>
</main>
</body>
</html>""")
    return "".join(parts)

def render_index_page(episodes):
    """渲染首頁（集數列表）。"""
    cards = []
    for ep in episodes:
        sentiment = (ep.get("sentiment") or "中性")
        s_class = {"看多":"bull","看空":"bear"}.get(sentiment, "neutral")
        cards.append(f"""
    <a class="ep-card {s_class}" href="episodes/{_esc(ep['episode'])}.html">
      <div class="ep-card-head">
        <span class="ep-num">{_esc(ep['episode'])}</span>
        <span class="ep-date">{_esc(ep.get('date') or '')}</span>
      </div>
      <h3>{_esc(ep.get('title',''))}</h3>
      <p class="ep-intro">{_esc((ep.get('intro') or '')[:120])}…</p>
      <div class="ep-card-foot">
        <span class="badge {s_class}">● {_esc(sentiment)}</span>
        <span class="muted">{_esc(ep.get('market_summary',''))}</span>
      </div>
    </a>""")

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(SITE_TITLE)}</title>
<link rel="stylesheet" href="assets/style.css">
</head>
<body>
<header class="topbar">
  <span class="brand">{_esc(SITE_TITLE)}</span>
  <span class="muted">{len(episodes)} 集</span>
</header>
<main class="container">
  <h1 class="home-title">所有集數</h1>
  <input id="search" class="search" type="search" placeholder="搜尋集數、標題或關鍵字...">
  <div class="ep-grid">
    {"".join(cards)}
  </div>
</main>
<script>
  const q = document.getElementById('search');
  q.addEventListener('input', () => {{
    const t = q.value.trim().toLowerCase();
    document.querySelectorAll('.ep-card').forEach(c => {{
      c.style.display = c.innerText.toLowerCase().includes(t) ? '' : 'none';
    }});
  }});
</script>
</body>
</html>"""

CSS = """
*{box-sizing:border-box}
body{font-family:'Helvetica Neue',Helvetica,Arial,'PingFang TC','Microsoft JhengHei',sans-serif;background:#f4f7f9;color:#333;margin:0}
.topbar{display:flex;justify-content:space-between;align-items:center;padding:14px 24px;background:#fff;border-bottom:1px solid #e8e8e8;position:sticky;top:0;z-index:10}
.brand{font-weight:700;color:#2c3e50;text-decoration:none;font-size:16px}
.ep-tag{background:#3498db;color:#fff;padding:3px 10px;border-radius:4px;font-weight:bold;font-size:13px}
.container{max-width:780px;margin:24px auto;padding:0 20px 60px}
.home-title{margin:0 0 16px;color:#2c3e50}
.search{width:100%;padding:10px 14px;border:1px solid #ddd;border-radius:8px;font-size:14px;margin-bottom:20px}
.ep-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}
.ep-card{display:block;background:#fff;border-radius:10px;padding:16px;text-decoration:none;color:inherit;border:1px solid #eee;transition:transform .15s,box-shadow .15s}
.ep-card:hover{transform:translateY(-2px);box-shadow:0 6px 18px rgba(0,0,0,.06)}
.ep-card-head{display:flex;justify-content:space-between;font-size:12px;color:#888;margin-bottom:8px}
.ep-num{background:#ecf0f1;padding:2px 8px;border-radius:4px;color:#2c3e50;font-weight:bold}
.ep-card h3{margin:6px 0;font-size:15px;color:#2c3e50;line-height:1.4}
.ep-intro{font-size:12px;color:#777;line-height:1.5;margin:6px 0}
.ep-card-foot{display:flex;align-items:center;gap:8px;margin-top:8px}
.ep-card.bull{border-top:3px solid #27ae60}
.ep-card.bear{border-top:3px solid #c0392b}
.ep-card.neutral{border-top:3px solid #7f8c8d}

.ep-header{border-bottom:2px solid #eee;padding-bottom:14px;margin-bottom:20px}
.date-pill{background:#3498db;color:#fff;padding:4px 10px;border-radius:4px;font-weight:bold;font-size:13px}
.ep-header h1{margin:10px 0 0;color:#2c3e50;font-size:22px}

.intro-box{background:#fff8e7;border-left:4px solid #f39c12;padding:14px 16px;border-radius:4px;margin-bottom:24px}
.intro-box h4{margin:0 0 6px;color:#f39c12;font-size:14px}
.intro-box p{margin:0;line-height:1.7;font-size:14px;color:#555}

.section-title{color:#2c3e50;border-bottom:1px solid #eee;padding-bottom:6px;margin-top:28px;font-size:15px}

.market-row{display:flex;align-items:center;gap:10px;padding:12px 16px;border-radius:8px;margin-bottom:20px}
.market-row.bull{background:#eaf6ee}
.market-row.bear{background:#fdecea}
.market-row.neutral{background:#f0f0f0}
.market-summary{font-size:15px;color:#2c3e50}

.badge{color:#fff;padding:3px 10px;border-radius:12px;font-size:12px;font-weight:bold;background:#7f8c8d}
.badge.bull{background:#27ae60}.badge.bear{background:#c0392b}.badge.watch{background:#f39c12}.badge.neutral{background:#7f8c8d}
.badge.cat-tw{background:#2980b9}.badge.cat-us{background:#8e44ad}.badge.cat-semi{background:#16a085}
.badge.cat-macro{background:#c0392b}.badge.cat-other{background:#7f8c8d}

.card{background:#fff;border:1px solid #e8e8e8;border-radius:8px;padding:14px;margin-bottom:12px}
.card-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.card.news p{margin:0 0 5px;font-size:13px;line-height:1.5;color:#555}
.muted{color:#888;font-size:12px}

.card.stock.bull{background:#f5fbf6}.card.stock.bear{background:#fdf6f6}.card.stock.watch{background:#fefaf2}
.stock-name{font-size:16px;color:#2980b9}
.ticker{font-size:12px;color:#999;margin-left:6px}
.market-tag{font-size:11px;color:#aaa;margin-left:4px}
.chips{display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap}
.chip{background:#f1f2f6;padding:3px 8px;border-radius:4px;font-size:12px;color:#555}
.chip.risk-high b{color:#c0392b}.chip.risk-mid b{color:#f39c12}.chip.risk-low b{color:#27ae60}
.stock-summary{margin:0;font-size:13px;line-height:1.6;color:#444}

.card.qa h4{margin:0 0 8px;color:#8e44ad;font-size:14px}
.qa-question{background:#f5f0ff;border-left:3px solid #9b59b6;padding:8px 12px;border-radius:0 6px 6px 0;margin-bottom:10px}
.qa-question p{margin:0;font-size:13px;color:#555;font-style:italic}
.point{margin:0 0 6px;font-size:13px;line-height:1.55;color:#444}
.point b{color:#2c3e50}
.qa-quote{margin-top:10px;padding:10px 14px;background:#fdf2e9;border-radius:6px;text-align:center;color:#d35400;font-size:14px}

.disclaimer{text-align:center;font-size:11px;color:#bbb;margin-top:30px}
"""

def write_site_assets():
    asset_dir = os.path.join(SITE_DIR, "assets")
    os.makedirs(asset_dir, exist_ok=True)
    css_path = os.path.join(asset_dir, "style.css")
    with open(css_path, "w", encoding="utf-8") as f:
        f.write(CSS)
    return css_path

def build_index(latest_data):
    """匯總所有已分析的集數，產生首頁與索引 JSON。"""
    items = []
    for fname in os.listdir(OUTPUT_DIR):
        if not fname.endswith("_analysis.json"):
            continue
        try:
            with open(os.path.join(OUTPUT_DIR, fname), "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        ep = d.get("episode") or fname.replace("_analysis.json", "")
        items.append({
            "episode":         ep,
            "date":            d.get("date") or _format_pub_date(d.get("pub_date")) or "",
            "title":           d.get("title", ""),
            "intro":           d.get("intro", ""),
            "sentiment":       (d.get("market_view") or {}).get("sentiment", "中性"),
            "market_summary":  (d.get("market_view") or {}).get("summary", ""),
        })

    # 按集數編號倒序（EP123 > EP122）
    def ep_key(x):
        m = re.search(r"\d+", x.get("episode", ""))
        return int(m.group(0)) if m else 0
    items.sort(key=ep_key, reverse=True)

    # 寫索引 JSON（給前端 / 其他工具用）
    dump_pretty_json(items, os.path.join(SITE_DATA_DIR, "episodes.json"))

    # 寫首頁
    with open(os.path.join(SITE_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_index_page(items))

def build_episode_page(data):
    """寫單集頁面 + 對應的 JSON 給前端取用。"""
    episode = data.get("episode", "EP")
    html_path = os.path.join(SITE_EPISODES_DIR, f"{episode}.html")
    json_path = os.path.join(SITE_DATA_DIR, f"{episode}.json")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_episode_page(data))
    dump_pretty_json(data, json_path)
    return html_path

# =========================
# 🚀 主流水線
# =========================
def main():
    print("🔥 啟動全自動分析流水線...")
    print(f"   FORCE_RERUN = {FORCE_RERUN}")

    episode_no   = os.environ.get("EPISODE_NO", "").strip()
    mp3_url      = os.environ.get("MP3_URL", "").strip()
    pub_date_raw = os.environ.get("PUB_DATE", "").strip()

    if not episode_no or not mp3_url:
        print("❌ 缺少 EPISODE_NO 或 MP3_URL")
        sys.exit(1)

    print(f"📻 集數：{episode_no}  |  發布：{pub_date_raw}")

    # 防重複
    last_ep = ""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            last_ep = f.read().strip()
    if episode_no == last_ep and not FORCE_RERUN:
        print(f"⏭️  {episode_no} 已處理過，本次略過（FORCE_RERUN=true 可強制重跑）")
        # 即使略過，也補一次首頁（萬一手動加了舊資料）
        build_index(latest_data=None)
        write_site_assets()
        sys.exit(0)

    # 下載 MP3
    mp3_path = os.path.join(OUTPUT_DIR, f"{episode_no}.mp3")
    if not os.path.exists(mp3_path):
        print(f"⬇️ 下載 {episode_no}.mp3 ...")
        r = requests.get(mp3_url, stream=True, timeout=120)
        with open(mp3_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        print("✅ MP3 下載完成")
    else:
        print("⚡ MP3 已存在")

    # 逐字稿
    transcript = get_full_transcript(mp3_path, episode_no)
    if len(transcript) < 100:
        raise ValueError("❌ 逐字稿太短，可能辨識失敗")

    # 分析
    parsed = analyze_from_transcript(transcript, pub_date_raw, episode_no)

    # 重新整理（保險：即便來自快取也照排序）
    parsed = normalize_payload(parsed, episode_no, pub_date_raw)
    dump_pretty_json(parsed, os.path.join(OUTPUT_DIR, f"{episode_no}_analysis.json"))

    # 渲染網站
    print("🎨 產生網站頁面...")
    write_site_assets()
    page_path = build_episode_page(parsed)
    build_index(latest_data=parsed)
    print(f"📄 集數頁面：{page_path}")
    print(f"🏠 首頁：{os.path.join(SITE_DIR, 'index.html')}")

    # 更新歷史
    with open(HISTORY_FILE, "w") as f:
        f.write(episode_no)

    print(f"🎉 {episode_no} 全部完成！")

if __name__ == "__main__":
    main()

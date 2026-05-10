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
    raw = re.sub(r

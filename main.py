import os
import re
import sys
import time
import json
import requests
import smtplib
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google import genai
from google.genai import types

# =========================
# ⚙️ 設定區（從環境變數讀取）
# =========================
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY")
SENDER_EMAIL    = os.environ.get("GMAIL_USER")
APP_PASSWORD    = os.environ.get("GMAIL_PASS")
RECEIVER_EMAIL  = os.environ.get("EMAIL_TO", SENDER_EMAIL)   # 若沒設定 EMAIL_TO，寄給自己
FORCE_RERUN     = os.environ.get("FORCE_RERUN", "false").lower() == "true"

client  = genai.Client(api_key=GEMINI_API_KEY)
RSS_URL = "https://open.firstory.me/rss/user/ck7t2i2ncqopi0873tqaimwq1"

# 儲存路徑（這些會被 commit 回 GitHub）
OUTPUT_DIR    = "output"
HISTORY_FILE  = "last_ep.txt"
os.makedirs(OUTPUT_DIR, exist_ok=True)

UNRESTRICTED_SAFETY = [
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,       threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,         threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,  threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,  threshold=types.HarmBlockThreshold.BLOCK_NONE),
]

# =========================
# 📡 模組 1：抓取最新 RSS
# =========================
def get_latest_episode_from_rss(rss_url):
    print("📡 正在抓取 RSS...")

    # ✅ 模擬真實 Podcast App，S3 不會擋這類 UA
    podcast_agents = [
        "Overcast/3.0 (+http://overcast.fm/; iOS podcast app)",
        "PocketCasts/7.0",
        "AppleCoreMedia/1.0.0.18G82 (iPhone; U; CPU iPhone OS 14_7 like Mac OS X)",
        "Castro/2022 (iOS 15.0)",
    ]

    # ✅ 同時試多個代理服務
    encoded = requests.utils.quote(rss_url, safe='')
    sources = [
        # 直接抓，用 Podcast App UA
        {"name": "direct_overcast",    "url": rss_url,                                              "ua": podcast_agents[0], "is_proxy": False},
        {"name": "direct_pocketcasts", "url": rss_url,                                              "ua": podcast_agents[1], "is_proxy": False},
        {"name": "direct_apple",       "url": rss_url,                                              "ua": podcast_agents[2], "is_proxy": False},
        # 代理服務
        {"name": "corsproxy",          "url": f"https://corsproxy.io/?{encoded}",                   "ua": "Mozilla/5.0",     "is_proxy": True},
        {"name": "codetabs",           "url": f"https://api.codetabs.com/v1/proxy?quest={encoded}", "ua": "Mozilla/5.0",     "is_proxy": True},
        {"name": "allorigins_raw",     "url": f"https://api.allorigins.win/raw?url={encoded}",      "ua": "Mozilla/5.0",     "is_proxy": True},
    ]

    xml_content = None

    for src in sources:
        try:
            print(f"   嘗試：{src['name']}...")
            r = requests.get(
                src["url"],
                timeout=20,
                headers={"User-Agent": src["ua"]},
                allow_redirects=True
            )
            print(f"   狀態碼：{r.status_code}")

            if r.status_code != 200:
                continue

            content = r.text
            if "<item>" in content or "<entry>" in content:
                print(f"   ✅ {src['name']} 成功")
                xml_content = content
                break
            else:
                print(f"   {src['name']} 回傳內容不含 RSS 項目")

        except Exception as e:
            print(f"   {src['name']} 失敗：{e}")
            continue

    if not xml_content:
        raise Exception("❌ 所有方式均失敗，請見上方 log")

    # 解析 XML
    root         = ET.fromstring(xml_content)
    latest_item  = root.find('.//channel/item')
    title        = latest_item.find('title').text
    mp3_url      = latest_item.find('enclosure').attrib['url']
    pub_date_raw = latest_item.findtext('pubDate', default='')

    if not mp3_url:
        raise Exception("❌ 找不到 MP3 連結")

    match      = re.search(r"(EP\d+)", title, re.IGNORECASE)
    episode_no = match.group(1).upper() if match else "LATEST_EP"

    print(f"✅ 集數：{episode_no}  |  發布時間：{pub_date_raw}")
    return episode_no, title, mp3_url, pub_date_raw
# =========================
# 📝 模組 2：音檔 → 逐字稿
# =========================
def get_full_transcript(mp3_path, episode_no):
    # ✅ 快取：若已有逐字稿就直接讀取，不重新跑 Gemini
    txt_path = os.path.join(OUTPUT_DIR, f"{episode_no}_transcript.txt")
    if os.path.exists(txt_path):
        print(f"⚡ 逐字稿快取命中，直接讀取：{txt_path}")
        with open(txt_path, "r", encoding="utf-8") as f:
            return f.read()

    print("☁️ 正在上傳音檔至 Google 伺服器...")
    audio_file = client.files.upload(file=mp3_path)

    while audio_file.state.name == "PROCESSING":
        time.sleep(5)
        audio_file = client.files.get(name=audio_file.name)

    if audio_file.state.name == "FAILED":
        raise ValueError("❌ 音檔處理失敗！")

    print("✍️ 開始產生逐字稿...")
    prompt = (
        "請根據這段音檔，整理出一份完整的繁體中文逐字稿。"
        "要求：盡可能一字不漏，加上標點符號與適當分段。"
        "請務必加上時間戳記，格式為「[分:秒] 文字」。"
        "不需做總結或摘要。"
    )
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[audio_file, prompt],
        config=types.GenerateContentConfig(safety_settings=UNRESTRICTED_SAFETY)
    )
    client.files.delete(name=audio_file.name)

    transcript = response.text.strip()

    # ✅ 儲存逐字稿（會被 commit 回 GitHub 永久保存）
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(transcript)
    print(f"💾 逐字稿已儲存：{txt_path}")

    return transcript


# =========================
# 🧠 模組 3：逐字稿 → JSON 分析
# =========================
def analyze_from_transcript(transcript_text, pub_date_raw, episode_no):
    # ✅ 快取：若已有分析結果就直接讀取
    json_path = os.path.join(OUTPUT_DIR, f"{episode_no}_analysis.json")
    if os.path.exists(json_path) and not FORCE_RERUN:
        print(f"⚡ 分析快取命中，直接讀取：{json_path}")
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)

    print("🧠 正在根據逐字稿進行結構化分析...")

    # ✅ 把 RSS 發布日期傳進 prompt，幫助 Gemini 正確填 date 欄位
    date_hint = f"此集 Podcast 的 RSS 發布時間為：{pub_date_raw}，請據此推斷並填入 date 欄位（格式 YYYY-MM-DD）。" if pub_date_raw else ""

    prompt = f"""
你是專業的股市投資人。
請根據以下逐字稿進行分析，用繁體中文輸出，只輸出純 JSON，不含任何說明文字或 markdown 符號。
{date_hint}

━━━━━━━━━━━━━━━━━━━━━━━━
【STEP 1｜段落識別】
━━━━━━━━━━━━━━━━━━━━━━━━
此 Podcast 固定四段結構：
① 葉配（業配廣告）      → 完全忽略
② 生活分享（日常閒聊）  → 完全忽略
③ 股市市場話題          → 核心，逐則完整分析
④ Q&A                  → 只保留投資相關提問

━━━━━━━━━━━━━━━━━━━━━━━━
【STEP 2｜欄位規則（請逐條遵守）】
━━━━━━━━━━━━━━━━━━━━━━━━

■ episode
  - 從逐字稿中主持人說的集數、節目標題或開場白擷取
  - 格式：EP + 數字，如 EP657
  - 若完全找不到填 null

■ date
  - 優先使用上方提供的 RSS 發布時間
  - 格式：YYYY-MM-DD
  - 若完全找不到填 null

■ title
  - 本集最能代表內容的標題，20字以內
  - 像財經媒體的文章標題，點出最大亮點

■ intro（導讀）
  - 只摘要「股市話題」+「投資相關Q&A」的內容
  - 完全不提葉配與生活分享
  - 150-200字，語氣像節目官方介紹文案
  - 結構：市場背景 → 本集核心話題 → Q&A主軸

■ market_view
  - sentiment 只能填：看多 / 看空 / 中性
  - summary：主持人對整體市場最核心的一句話判斷，20字以內
    ✅ 好例子：「搶錢行情，多頭應咬住漲幅不縮手」
    ❌ 壞例子：「大盤進入混沌局面，類股輪動快速，資金流向難判斷」（太模糊）

■ news（新聞卡片）
  - 每個獨立話題/個股討論 = 一張卡，不要合併
  - title：8字以內，像報紙斗大標題，要有衝擊感
    ✅ 「台積電外資大賣壓」「Intel封裝良率衝90%」
    ❌ 「Meta簽AWS Graviton」（太像技術術語，不像標題）
  - category 只能選：台股 / 美股 / 半導體 / 總經 / 其他
  - event：客觀陳述發生什麼事，不含主觀評價，2句
  - view：主持人的解讀與看法，保留他的語氣與個性，2-3句
    （他慣用「我覺得」「說實在」「這個角度來看」等語氣）

■ stocks（個股卡片）
  - 只收錄主持人明確給出投資論點的個股，純路過提及不算
  - ticker：台股填數字代號（如 2330），美股填英文（如 INTC）
    ⚠️ 台積電 = 2330，聯發科 = 2454，絕對不能填 null
    若真的不知道代號才填 null
  - market 只能選：台股半導體 / 美股半導體 / 台股網通 / 台股其他 / 美股其他
  - summary：200字以內，包含：
    (a) 主持人看多/觀望/看空的主要理由
    (b) 短線觀察重點
    (c) 最大風險提示
  - catalyst_short：10字以內的短線催化劑，若無填 null
  - risk_note：15字以內，最具體的一個風險

■ qa（Q&A卡片）
  - title：15字以內，要聳動、有畫面感、讓人想點開
    ✅ 「長輩廢話不如直接送錢」「3000萬本金你會怎麼玩？」
    ❌ 「主動型ETF能打敗巴菲特魔咒？」（太學術）
  - question：精煉聽眾問題為1-2句話的核心提問
    ⚠️ 不要把逐字稿整段貼上，要整理成乾淨的問題
  - points：3-4個重點，每個 label 4字以內（像小標題）
  - quote：主持人最精華金句，原文照錄
    必須是有哲理、有衝擊感、能獨立成句的話

━━━━━━━━━━━━━━━━━━━━━━━━
【STEP 3｜Q&A 過濾標準（嚴格執行）】
━━━━━━━━━━━━━━━━━━━━━━━━
✅ 納入：選股/持股/賣股時機、個股產業看法、投資心法、倉位管理、槓桿維持率、總經判斷
❌ 跳過：純生活問題、與金融無關的個人意見、問題不明確且主持人未給出投資建議

━━━━━━━━━━━━━━━━━━━━━━━━
【輸出 JSON 結構】
━━━━━━━━━━━━━━━━━━━━━━━━
{{
  "episode": "EP657",
  "date": "YYYY-MM-DD 或 null",
  "title": "本集標題20字以內",
  "segment_map": {{
    "葉配":     "約第X-Y分鐘，若無填null",
    "生活分享": "約第X-Y分鐘",
    "股市話題": "約第X-Y分鐘",
    "QA":       "約第X-Y分鐘"
  }},
  "intro": "150-200字導讀，只含投資內容",
  "market_view": {{
    "sentiment": "看多|看空|中性",
    "summary": "20字以內，有力的一句話"
  }},
  "news": [
    {{
      "title": "8字以內衝擊標題",
      "category": "台股|美股|半導體|總經|其他",
      "source_range": "如 12-13",
      "event": "客觀事件2句，只有事實",
      "view": "主持人觀點2-3句，保留語氣個性"
    }}
  ],
  "stocks": [
    {{
      "name": "股票中文名稱",
      "ticker": "2330 或 INTC",
      "market": "台股半導體|美股半導體|台股網通|台股其他|美股其他",
      "sentiment": "看多|觀望|看空",
      "risk": "低|中|高",
      "source_range": "如 12-13",
      "summary": "200字以內，含看法理由+觀察重點+風險",
      "catalyst_short": "10字以內或null",
      "risk_note": "15字以內最具體的風險",
      "price": null,
      "rsi": null,
      "change_1m": null,
      "pe": null,
      "holding": "持倉損益百分比或null"
    }}
  ],
  "qa": [
    {{
      "title": "聳動標題15字以內",
      "source_range": "如 29-31",
      "question": "精煉後的核心提問1-2句",
      "points": [
        {{
          "label": "4字標籤",
          "content": "說明2-3句"
        }}
      ],
      "quote": "有衝擊感的金句原文20-40字"
    }}
  ]
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
            safety_settings=UNRESTRICTED_SAFETY
        )
    )

    # ✅ 防呆：移除可能的 markdown 包裝再解析
    raw  = response.text.strip()
    raw  = re.sub(r"^```(?:json)?", "", raw, flags=re.IGNORECASE).strip()
    raw  = re.sub(r"```$", "", raw).strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"❌ JSON 解析失敗：{e}")
        print("原始內容前 300 字：", raw[:300])
        raise

    # ✅ 儲存 JSON（會被 commit 回 GitHub 永久保存）
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(parsed, f, ensure_ascii=False, indent=2)
    print(f"💾 分析結果已儲存：{json_path}")

    return parsed


# =========================
# 💌 模組 4：JSON → HTML Email
# =========================
def generate_html_email(data):
    episode_str = data.get('episode') or ""
    date_str    = data.get('date')    or "最新市場快訊"
    title_str   = data.get('title')   or "市場重點整理"

    header_label = f"{episode_str}  {date_str}".strip() if episode_str else date_str

    html = f"""
<html>
<body style="font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;background:#f4f7f9;padding:20px;color:#333;">
<div style="max-width:650px;margin:auto;background:#fff;padding:30px;border-radius:12px;box-shadow:0 4px 15px rgba(0,0,0,.05);">

  <!-- Header -->
  <div style="border-bottom:2px solid #eee;padding-bottom:15px;margin-bottom:20px;">
    <span style="background:#3498db;color:#fff;padding:4px 10px;border-radius:4px;font-weight:bold;font-size:13px;">{header_label}</span>
    <h2 style="margin:10px 0 0;color:#2c3e50;font-size:20px;">{title_str}</h2>
  </div>

  <!-- 導讀 -->
  <div style="background:#f8f9fa;border-left:4px solid #f39c12;padding:15px;border-radius:4px;margin-bottom:25px;">
    <h4 style="margin:0 0 8px;color:#f39c12;font-size:14px;">💡 核心摘要</h4>
    <p style="margin:0;line-height:1.7;font-size:14px;color:#555;">{data.get('intro','')}</p>
  </div>
"""

    # 大盤觀點
    market      = data.get('market_view', {})
    sentiment   = market.get('sentiment', '中性')
    mkt_color   = {"看多": "#27ae60", "看空": "#c0392b"}.get(sentiment, "#7f8c8d")
    dot_bg      = {"看多": "#eaf6ee", "看空": "#fdecea"}.get(sentiment, "#f0f0f0")
    html += f"""
  <h3 style="color:#2c3e50;border-bottom:1px solid #eee;padding-bottom:5px;font-size:15px;">📊 大盤觀點</h3>
  <div style="background:{dot_bg};border-radius:8px;padding:12px 16px;margin-bottom:25px;display:flex;align-items:center;gap:10px;">
    <span style="background:{mkt_color};color:#fff;padding:3px 10px;border-radius:12px;font-size:13px;font-weight:bold;white-space:nowrap;">● {sentiment}</span>
    <span style="font-size:15px;color:#2c3e50;">{market.get('summary','')}</span>
  </div>
"""

    # 市場事件
    if data.get('news'):
        html += """<h3 style="color:#2c3e50;border-bottom:1px solid #eee;padding-bottom:5px;margin-top:25px;font-size:15px;">📰 市場事件與解析</h3>"""
        for news in data['news']:
            cat_colors = {"台股":"#2980b9","美股":"#8e44ad","半導體":"#16a085","總經":"#c0392b","其他":"#7f8c8d"}
            cat_color  = cat_colors.get(news.get('category',''), '#7f8c8d')
            html += f"""
  <div style="border:1px solid #e8e8e8;border-radius:8px;padding:14px;margin-bottom:12px;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
      <b style="font-size:15px;color:#2c3e50;">📌 {news.get('title','')}</b>
      <span style="background:{cat_color};color:#fff;font-size:11px;padding:2px 8px;border-radius:4px;">{news.get('category','')}</span>
    </div>
    <p style="margin:0 0 5px;font-size:13px;color:#666;line-height:1.5;"><b style="color:#555;">事件：</b>{news.get('event','')}</p>
    <p style="margin:0;font-size:13px;color:#2c3e50;line-height:1.5;"><b>深度解析：</b>{news.get('view','')}</p>
  </div>"""

    # 個股標的
    if data.get('stocks'):
        html += """<h3 style="color:#2c3e50;border-bottom:1px solid #eee;padding-bottom:5px;margin-top:25px;font-size:15px;">📈 標的動態</h3>"""
        for stock in data['stocks']:
            s         = stock.get('sentiment','觀望')
            s_color   = {"看多":"#27ae60","看空":"#c0392b"}.get(s,"#f39c12")
            s_bg      = {"看多":"#eaf6ee","看空":"#fdecea"}.get(s,"#fef9ec")
            risk      = stock.get('risk','')
            r_color   = {"高":"#c0392b","中":"#f39c12","低":"#27ae60"}.get(risk,"#888")
            ticker    = f" ({stock.get('ticker')})" if stock.get('ticker') else ""
            catalyst  = stock.get('catalyst_short') or "—"
            risk_note = stock.get('risk_note')      or "—"
            html += f"""
  <div style="border:1px solid #e8e8e8;border-radius:8px;padding:14px;margin-bottom:12px;background:{s_bg}08;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
      <div>
        <b style="font-size:16px;color:#2980b9;">{stock.get('name','')}</b>
        <span style="font-size:12px;color:#999;margin-left:6px;">{ticker.strip()}</span>
        <span style="font-size:11px;color:#aaa;margin-left:4px;">{stock.get('market','')}</span>
      </div>
      <span style="background:{s_color};color:#fff;padding:3px 10px;border-radius:12px;font-size:12px;font-weight:bold;">{s}</span>
    </div>
    <div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap;">
      <span style="background:#f1f2f6;padding:2px 8px;border-radius:4px;font-size:12px;color:#555;">⚠️ 風險：<b style="color:{r_color};">{risk}</b></span>
      <span style="background:#f1f2f6;padding:2px 8px;border-radius:4px;font-size:12px;color:#555;">🚀 催化劑：{catalyst}</span>
      <span style="background:#f1f2f6;padding:2px 8px;border-radius:4px;font-size:12px;color:#555;">🛡️ 風險點：{risk_note}</span>
    </div>
    <p style="margin:0;font-size:13px;line-height:1.6;color:#444;">{stock.get('summary','')}</p>
  </div>"""

    # Q&A
    if data.get('qa'):
        html += """<h3 style="color:#2c3e50;border-bottom:1px solid #eee;padding-bottom:5px;margin-top:25px;font-size:15px;">💼 投資策略與實務探討</h3>"""
        for qa in data['qa']:
            points_html = ""
            for pt in qa.get('points', []):
                points_html += f"""<p style="margin:0 0 6px 0;font-size:13px;line-height:1.5;">• <b style="color:#2c3e50;">{pt.get('label','')}：</b>{pt.get('content','')}</p>"""
            html += f"""
  <div style="border:1px solid #e8e8e8;border-radius:8px;padding:14px;margin-bottom:15px;">
    <h4 style="margin:0 0 8px;color:#8e44ad;font-size:14px;">💬 {qa.get('title','')}</h4>
    <div style="background:#f5f0ff;border-left:3px solid #9b59b6;padding:8px 12px;border-radius:0 6px 6px 0;margin-bottom:10px;">
      <p style="margin:0;font-size:13px;color:#555;font-style:italic;">{qa.get('question','')}</p>
    </div>
    {points_html}
    <div style="margin-top:10px;padding:10px 14px;background:#fdf2e9;border-radius:6px;text-align:center;">
      <b style="color:#d35400;font-size:14px;">「{qa.get('quote','')}」</b>
    </div>
  </div>"""

    html += """
  <p style="text-align:center;font-size:11px;color:#bbb;margin-top:30px;">投資組合終端・僅供參考不構成投資建議</p>
</div>
</body>
</html>"""
    return html


# =========================
# 📤 模組 5：寄信
# =========================
def send_email(html_content, subject_title):
    print("📧 正在連線至 Gmail 寄信...")
    msg            = MIMEMultipart()
    msg['Subject'] = f"📊 {subject_title}"
    msg['From']    = SENDER_EMAIL
    msg['To']      = RECEIVER_EMAIL
    msg.attach(MIMEText(html_content, 'html'))

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(SENDER_EMAIL, APP_PASSWORD)
            server.send_message(msg)
        print(f"✅ 信件已寄出至 {RECEIVER_EMAIL}")
    except Exception as e:
        print(f"❌ 寄信失敗：{e}")
        raise


# =========================
# 🚀 主流水線
# =========================
def main():
    print("🔥 啟動全自動分析流水線...")
    print(f"   FORCE_RERUN = {FORCE_RERUN}")

    # 1. 抓取最新集數（含發布日期）
    episode_no   = os.environ.get("EPISODE_NO", "").strip()
    mp3_url      = os.environ.get("MP3_URL", "").strip()
    pub_date_raw = os.environ.get("PUB_DATE", "").strip()
    rss_title    = episode_no
    
    if not episode_no or not mp3_url:
        print("❌ 缺少 EPISODE_NO 或 MP3_URL")
        sys.exit(1)
    
    print(f"📻 收到集數：{episode_no}  |  {pub_date_raw}")

    # 2. 防重複：已處理過且非強制重跑 → 直接結束
    last_ep = ""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            last_ep = f.read().strip()

    if episode_no == last_ep and not FORCE_RERUN:
        print(f"⏭️  {episode_no} 已處理過，本次略過（傳入 FORCE_RERUN=true 可強制重跑）")
        sys.exit(0)

    print(f"🆕 發現新集數：{episode_no}，開始處理...")

    # 3. 下載 MP3（暫存於 GitHub Actions runner，用完即消失）
    mp3_path = os.path.join(OUTPUT_DIR, f"{episode_no}.mp3")
    if not os.path.exists(mp3_path):
        print(f"⬇️ 下載 {episode_no}.mp3 ...")
        r = requests.get(mp3_url, stream=True, timeout=120)
        with open(mp3_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        print("✅ MP3 下載完成")
    else:
        print("⚡ MP3 已存在，跳過下載")

    # 4. 音檔 → 逐字稿（有快取）
    transcript = get_full_transcript(mp3_path, episode_no)
    if len(transcript) < 100:
        raise ValueError("❌ 逐字稿內容太短，可能辨識失敗")

    # 5. 逐字稿 → JSON 分析（有快取）
    parsed_data = analyze_from_transcript(transcript, pub_date_raw, episode_no)

    # 6. 渲染 HTML 並寄信
    print("🎨 渲染 Email...")
    email_html   = generate_html_email(parsed_data)
    subject      = f"股癌 {parsed_data.get('episode', episode_no)} | {parsed_data.get('title', rss_title)}"
    send_email(email_html, subject)

    # 7. 更新歷史紀錄（這個檔案會被 commit 回 GitHub）
    with open(HISTORY_FILE, "w") as f:
        f.write(episode_no)
    print(f"🎉 {episode_no} 全部完成！")


if __name__ == "__main__":
    main()

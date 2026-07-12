import os
import re
import sys
import time
import json
import html as html_lib
import requests
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import parsedate_to_datetime
from google import genai
from google.genai import types

# =========================
# ⚙️ 設定區（從環境變數讀取）
# =========================
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY")
SENDER_EMAIL    = os.environ.get("GMAIL_USER")
APP_PASSWORD    = os.environ.get("GMAIL_PASS")
RECEIVER_EMAIL  = os.environ.get("EMAIL_TO", SENDER_EMAIL)
FORCE_RERUN     = os.environ.get("FORCE_RERUN", "false").lower() == "true"
SITE_TITLE      = os.environ.get("SITE_TITLE", "股市筆記")

client = genai.Client(api_key=GEMINI_API_KEY)

# 儲存路徑
OUTPUT_DIR        = "output"
SITE_DIR          = "site"
SITE_EPISODES_DIR = os.path.join(SITE_DIR, "episodes")
SITE_DATA_DIR     = os.path.join(SITE_DIR, "data")
HISTORY_FILE      = "last_ep.txt"

for d in (OUTPUT_DIR, SITE_DIR, SITE_EPISODES_DIR, SITE_DATA_DIR):
    os.makedirs(d, exist_ok=True)

UNRESTRICTED_SAFETY = [
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,       threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,         threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,  threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,  threshold=types.HarmBlockThreshold.BLOCK_NONE),
]

# =========================
# 📝 模組 1：音檔 → 逐字稿
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
# 🧠 模組 2：逐字稿 → JSON 分析
# =========================
def analyze_from_transcript(transcript_text, pub_date_raw, episode_no):
    json_path = os.path.join(OUTPUT_DIR, f"{episode_no}_analysis.json")
    if os.path.exists(json_path) and not FORCE_RERUN:
        print(f"⚡ 分析快取命中：{json_path}")
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)

    print("🧠 結構化分析中...")
    date_hint = (
        f"此集 RSS 發布時間為：{pub_date_raw}，請據此推斷並填入 date 欄位（格式 YYYY-MM-DD）。"
        if pub_date_raw else ""
    )

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
✅ 應使用：客觀、第三人稱的財經媒體語氣（「市場觀點」「據悉」「值得關注」）
❌ 絕對不能出現：分析師、主持人、來賓、節目、本集、集數、聽眾、podcast、節目中、本期、這集、上集、下集、時間戳記、分鐘數、來源標記

━━━━━━━━━━━━━━━━━━━━━━━━
【STEP 3｜欄位規則】
━━━━━━━━━━━━━━━━━━━━━━━━

■ date：YYYY-MM-DD，無法判斷填 null
■ title：20字內財經頭條
■ intro：500字市場摘要（市場背景→核心話題→投資議題概覽），不提任何 podcast 相關字眼

■ market_view
  - sentiment 只能填：看多 / 看空 / 中性
  - summary：20字以內市場核心判斷
    ✅ 「資金回流科技股，多頭格局持續確立」

■ news（市場事件卡片）
  - 每個獨立事件 = 一張卡，不合併
  - title：8字以內，報紙斗大標題風格
  - category 只能選：台股 / 美股 / 半導體 / 總經 / 其他
  - event：客觀陳述事件本身，2句，只有事實
  - view：市場分析觀點，2-3句，第三人稱客觀語氣

■ stocks（個股觀點卡片）
  - 被提及的個股/產業族群都要收錄，最少 3 檔
  - 優先順序：討論最多 > 有明確建議 > 簡單評論
  - ticker：台股填數字（2330），美股填英文（INTC）
    ⚠️ 台積電=2330，聯發科=2454，已知代號不能填 null
  - market 只能選：台股半導體 / 美股半導體 / 台股網通 / 台股其他 / 美股其他
  - summary：4-6句詳細分析，含(a)核心理由(b)短線觀察(c)風險
  - catalyst_short：10字以內，若無填 null
  - risk_note：15字以內，最具體的風險描述

■ host_disclosure（持倉揭露）★★★ 最重要
  - 只收錄分析師本人明確提到「自己」的操作，不包含對外建議
  - action 只能填：持有 / 已買入 / 已出清 / 加碼中 / 考慮買入 / 考慮出清
  - note：操作理由或原話，30字以內，保留原文語氣
  - 若完全沒提到自身持倉，輸出空陣列 []
    ✅ 「已買入，等待訂單量級確認後加碼」
    ❌ 「建議大家可以買」（這是建議，不算持倉揭露）

■ qa（投資議題探討卡片）
  - title：15字以內，聳動有畫面感
    ✅ 「3000萬本金你會怎麼配置？」
  - question：精煉為1-2句核心提問
  - points：3-4個重點，label 4字以內
  - 優先順序：討論最多 > 有明確建議 > 簡單評論
  - quote：最具代表性的觀點金句，20-40字，有哲理或衝擊感

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
  "intro": "500字市場摘要",
  "market_view": {{
    "sentiment": "看多|看空|中性",
    "summary": "20字以內市場核心判斷"
  }},
  "news": [
    {{
      "title": "8字以內事件標題",
      "category": "台股|美股|半導體|總經|其他",
      "event": "客觀事件描述2句",
      "view": "市場分析觀點2-3句，第三人稱"
    }}
  ],
  "stocks": [
    {{
      "name": "股票中文名稱",
      "ticker": "代號",
      "market": "台股半導體|美股半導體|台股網通|台股其他|美股其他",
      "sentiment": "看多|觀望|看空",
      "risk": "低|中|高",
      "summary": "4-6句投資論點",
      "catalyst_short": "10字以內或null",
      "risk_note": "15字以內具體風險",
      "price": null,
      "rsi": null,
      "change_1m": null,
      "pe": null,
      "holding": null
    }}
  ],
  "host_disclosure": [
    {{
      "name": "股票中文名稱",
      "ticker": "代號或null",
      "action": "持有|已買入|已出清|加碼中|考慮買入|考慮出清",
      "note": "操作理由或原話，30字以內"
    }}
  ],
  "qa": [
    {{
      "title": "聳動標題15字以內",
      "question": "精煉核心提問1-2句",
      "points": [
        {{
          "label": "4字標籤",
          "content": "說明2-3句，客觀語氣"
        }}
      ],
      "quote": "精華觀點金句20-40字"
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

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(parsed, f, ensure_ascii=False, indent=2)
    print(f"💾 分析結果已儲存：{json_path}")
    return parsed


# =========================
# 💌 模組 3：JSON → HTML Email
# =========================
def generate_html_email(data, episode_no):
    date_str  = data.get('date') or "最新市場快訊"
    title_str = data.get('title') or "市場重點整理"

    html = f"""
<html>
<body style="font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;background:#f4f7f9;padding:20px;color:#333;">
<div style="max-width:650px;margin:auto;background:#fff;padding:30px;border-radius:12px;box-shadow:0 4px 15px rgba(0,0,0,.05);">
  <div style="border-bottom:2px solid #eee;padding-bottom:15px;margin-bottom:20px;">
    <span style="background:#3498db;color:#fff;padding:4px 10px;border-radius:4px;font-weight:bold;font-size:13px;">{date_str}</span>
    <h2 style="margin:10px 0 0;color:#2c3e50;font-size:20px;">{title_str}</h2>
  </div>
  <div style="background:#f8f9fa;border-left:4px solid #f39c12;padding:15px;border-radius:4px;margin-bottom:25px;">
    <h4 style="margin:0 0 8px;color:#f39c12;font-size:14px;">💡 本週市場摘要</h4>
    <p style="margin:0;line-height:1.7;font-size:14px;color:#555;">{data.get('intro','')}</p>
  </div>
"""
    # 大盤觀點
    market    = data.get('market_view', {})
    sentiment = market.get('sentiment', '中性')
    mkt_color = {"看多": "#27ae60", "看空": "#c0392b"}.get(sentiment, "#7f8c8d")
    dot_bg    = {"看多": "#eaf6ee", "看空": "#fdecea"}.get(sentiment, "#f0f0f0")
    html += f"""
  <h3 style="color:#2c3e50;border-bottom:1px solid #eee;padding-bottom:5px;font-size:15px;">📊 市場觀點</h3>
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
    <p style="margin:0;font-size:13px;color:#2c3e50;line-height:1.5;"><b>市場解讀：</b>{news.get('view','')}</p>
  </div>"""

    # 個股觀點
    if data.get('stocks'):
        html += """<h3 style="color:#2c3e50;border-bottom:1px solid #eee;padding-bottom:5px;margin-top:25px;font-size:15px;">📈 個股觀點</h3>"""
        for stock in data['stocks']:
            s         = stock.get('sentiment','觀望')
            s_color   = {"看多":"#27ae60","看空":"#c0392b"}.get(s,"#f39c12")
            s_bg      = {"看多":"#eaf6ee","看空":"#fdecea"}.get(s,"#fef9ec")
            risk      = stock.get('risk','')
            r_color   = {"高":"#c0392b","中":"#f39c12","低":"#27ae60"}.get(risk,"#888")
            ticker    = f"({stock.get('ticker')})" if stock.get('ticker') else ""
            catalyst  = stock.get('catalyst_short') or "—"
            risk_note = stock.get('risk_note') or "—"
            html += f"""
  <div style="border:1px solid #e8e8e8;border-radius:8px;padding:14px;margin-bottom:12px;background:{s_bg}08;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
      <div>
        <b style="font-size:16px;color:#2980b9;">{stock.get('name','')}</b>
        <span style="font-size:12px;color:#999;margin-left:6px;">{ticker}</span>
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

    # ★★★ 持倉揭露
    if data.get('host_disclosure'):
        html += """<h3 style="color:#2c3e50;border-bottom:1px solid #eee;padding-bottom:5px;margin-top:25px;font-size:15px;">🔍 持倉揭露</h3>"""
        html += """<div style="background:#fffbf0;border:1.5px solid #f39c12;border-radius:8px;padding:14px;margin-bottom:12px;">"""
        html += """<p style="margin:0 0 10px;font-size:12px;color:#e67e22;font-weight:bold;">⚠️ 以下為分析師本人實際操作揭露，僅供參考</p>"""
        for disc in data['host_disclosure']:
            action = disc.get('action', '')
            action_color = {
                "已買入":   "#27ae60",
                "加碼中":   "#27ae60",
                "考慮買入": "#2980b9",
                "持有":     "#7f8c8d",
                "考慮出清": "#e67e22",
                "已出清":   "#c0392b",
            }.get(action, "#7f8c8d")
            ticker = f"({disc.get('ticker')})" if disc.get('ticker') else ""
            html += f"""
  <div style="display:flex;align-items:flex-start;gap:10px;padding:8px 0;border-bottom:1px solid #f0e6c8;">
    <span style="background:{action_color};color:#fff;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:bold;white-space:nowrap;flex-shrink:0;">{action}</span>
    <div>
      <b style="font-size:14px;color:#2c3e50;">{disc.get('name','')}</b>
      <span style="font-size:12px;color:#999;margin-left:5px;">{ticker}</span>
      <p style="margin:3px 0 0;font-size:13px;color:#555;line-height:1.5;">{disc.get('note','')}</p>
    </div>
  </div>"""
        html += """</div>"""

    # 投資議題
    if data.get('qa'):
        html += """<h3 style="color:#2c3e50;border-bottom:1px solid #eee;padding-bottom:5px;margin-top:25px;font-size:15px;">💼 投資議題探討</h3>"""
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
  <p style="text-align:center;font-size:11px;color:#bbb;margin-top:30px;">僅供參考，不構成投資建議</p>
</div>
</body>
</html>"""
    return html


# =========================
# 📤 模組 4：寄信
# =========================

def send_email(html_content, subject_title):
    print("📧 正在連線至 Gmail 寄信...")
    
    # ✅ 支援多個收件人（逗號分隔）
    receivers = [r.strip() for r in RECEIVER_EMAIL.split(",") if r.strip()]
    
    msg            = MIMEMultipart()
    msg['Subject'] = f"📊 {subject_title}"
    msg['From']    = SENDER_EMAIL
    msg['To']      = ", ".join(receivers)   # ✅ 顯示所有收件人
    msg.attach(MIMEText(html_content, 'html'))

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(SENDER_EMAIL, APP_PASSWORD)
            server.sendmail(SENDER_EMAIL, receivers, msg.as_string())  # ✅ 實際寄給所有人
        print(f"✅ 信件已寄出至 {', '.join(receivers)}")
    except Exception as e:
        print(f"❌ 寄信失敗：{e}")
        raise

# =========================
# 🌐 模組 5：JSON → 靜態網站
# =========================
def _esc(x):
    return html_lib.escape("" if x is None else str(x), quote=True)

def _format_pub_date(pub_date_raw):
    if not pub_date_raw:
        return ""
    try:
        dt = parsedate_to_datetime(pub_date_raw)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return pub_date_raw

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
.ep-card.bull{border-top:3px solid #27ae60}.ep-card.bear{border-top:3px solid #c0392b}.ep-card.neutral{border-top:3px solid #7f8c8d}
.ep-header{border-bottom:2px solid #eee;padding-bottom:14px;margin-bottom:20px}
.date-pill{background:#3498db;color:#fff;padding:4px 10px;border-radius:4px;font-weight:bold;font-size:13px}
.ep-header h1{margin:10px 0 0;color:#2c3e50;font-size:22px}
.intro-box{background:#fff8e7;border-left:4px solid #f39c12;padding:14px 16px;border-radius:4px;margin-bottom:24px}
.intro-box h4{margin:0 0 6px;color:#f39c12;font-size:14px}
.intro-box p{margin:0;line-height:1.7;font-size:14px;color:#555}
.section-title{color:#2c3e50;border-bottom:1px solid #eee;padding-bottom:6px;margin-top:28px;font-size:15px}
.market-row{display:flex;align-items:center;gap:10px;padding:12px 16px;border-radius:8px;margin-bottom:20px}
.market-row.bull{background:#eaf6ee}.market-row.bear{background:#fdecea}.market-row.neutral{background:#f0f0f0}
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
.disclosure-box{background:#fffbf0;border:1.5px solid #f39c12;border-radius:8px;padding:14px;margin-bottom:12px}
.disclosure-warning{margin:0 0 10px;font-size:12px;color:#e67e22;font-weight:bold}
.disclosure-row{display:flex;align-items:flex-start;gap:10px;padding:8px 0;border-bottom:1px solid #f0e6c8}
.disclosure-row:last-child{border-bottom:none}
.disc-action{color:#fff;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:bold;white-space:nowrap;flex-shrink:0}
.disc-action.buy,.disc-action.add{background:#27ae60}
.disc-action.consider-buy{background:#2980b9}
.disc-action.hold{background:#7f8c8d}
.disc-action.consider-sell{background:#e67e22}
.disc-action.sold{background:#c0392b}
.disc-name{font-size:14px;color:#2c3e50;font-weight:bold}
.disc-ticker{font-size:12px;color:#999;margin-left:5px}
.disc-note{margin:3px 0 0;font-size:13px;color:#555;line-height:1.5}
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
    with open(os.path.join(asset_dir, "style.css"), "w", encoding="utf-8") as f:
        f.write(CSS)

def render_episode_page(data):
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

    market    = data.get("market_view") or {}
    sentiment = market.get("sentiment", "中性")
    s_class   = {"看多": "bull", "看空": "bear"}.get(sentiment, "neutral")
    parts.append(f"""
  <h3 class="section-title">📊 市場觀點</h3>
  <div class="market-row {s_class}">
    <span class="badge {s_class}">● {_esc(sentiment)}</span>
    <span class="market-summary">{_esc(market.get('summary',''))}</span>
  </div>
""")

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

    if data.get("stocks"):
        parts.append('<h3 class="section-title">📈 個股觀點</h3>')
        for s in data["stocks"]:
            sent       = s.get("sentiment", "觀望")
            sent_class = {"看多":"bull","看空":"bear"}.get(sent, "watch")
            risk       = s.get("risk", "")
            risk_class = {"高":"risk-high","中":"risk-mid","低":"risk-low"}.get(risk, "")
            ticker     = f"({_esc(s.get('ticker'))})" if s.get("ticker") else ""
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

    if data.get("host_disclosure"):
        parts.append('<h3 class="section-title">🔍 持倉揭露</h3>')
        parts.append('<div class="disclosure-box">')
        parts.append('<p class="disclosure-warning">⚠️ 以下為分析師本人實際操作揭露，僅供參考</p>')
        action_class_map = {
            "已買入":"buy","加碼中":"add","考慮買入":"consider-buy",
            "持有":"hold","考慮出清":"consider-sell","已出清":"sold",
        }
        for disc in data["host_disclosure"]:
            action = disc.get("action", "")
            ac     = action_class_map.get(action, "hold")
            ticker = f"({_esc(disc.get('ticker'))})" if disc.get("ticker") else ""
            parts.append(f"""
  <div class="disclosure-row">
    <span class="disc-action {ac}">{_esc(action)}</span>
    <div>
      <b class="disc-name">{_esc(disc.get('name',''))}</b>
      <span class="disc-ticker">{ticker}</span>
      <p class="disc-note">{_esc(disc.get('note',''))}</p>
    </div>
  </div>""")
        parts.append('</div>')

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
    cards = []
    for ep in episodes:
        sentiment = ep.get("sentiment") or "中性"
        s_class   = {"看多":"bull","看空":"bear"}.get(sentiment, "neutral")
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
  <div class="ep-grid">{"".join(cards)}</div>
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

def build_index():
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
            "episode":        ep,
            "date":           d.get("date") or _format_pub_date(d.get("pub_date")) or "",
            "title":          d.get("title", ""),
            "intro":          d.get("intro", ""),
            "sentiment":      (d.get("market_view") or {}).get("sentiment", "中性"),
            "market_summary": (d.get("market_view") or {}).get("summary", ""),
        })
    def ep_key(x):
        m = re.search(r"\d+", x.get("episode", ""))
        return int(m.group(0)) if m else 0
    items.sort(key=ep_key, reverse=True)
    with open(os.path.join(SITE_DATA_DIR, "episodes.json"), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    with open(os.path.join(SITE_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_index_page(items))

def build_episode_page(data):
    episode   = data.get("episode", "EP")
    html_path = os.path.join(SITE_EPISODES_DIR, f"{episode}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_episode_page(data))
    return html_path


# =========================
# 🚀 主流水線
# =========================
def main():
    print("🔥 啟動全自動分析流水線...")
    print(f"   FORCE_RERUN = {FORCE_RERUN}")

    # ✅ 一律從 GAS 傳入的環境變數讀取，不自己抓 RSS
    episode_no   = os.environ.get("EPISODE_NO", "").strip()
    mp3_url      = os.environ.get("MP3_URL", "").strip()
    pub_date_raw = os.environ.get("PUB_DATE", "").strip()

    if not episode_no or not mp3_url:
        print("❌ 缺少 EPISODE_NO 或 MP3_URL，請確認 GAS 有正確傳入資料")
        sys.exit(1)

    print(f"📻 集數：{episode_no}  |  發布：{pub_date_raw}")

    # 防重複
    last_ep = ""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            last_ep = f.read().strip()
    if episode_no == last_ep and not FORCE_RERUN:
        print(f"⏭️  {episode_no} 已處理過，略過（FORCE_RERUN=true 可強制重跑）")
        sys.exit(0)

    # 下載 MP3
    mp3_path = os.path.join(OUTPUT_DIR, f"{episode_no}.mp3")
    if not os.path.exists(mp3_path):
        print(f"⬇️ 下載 {episode_no}.mp3 ...")
        r = requests.get(mp3_url, stream=True, timeout=180,
                         headers={"User-Agent": "Mozilla/5.0"})
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
    parsed.setdefault("episode", episode_no)

    # 寄信
    print("📧 寄送 Email...")
    email_html = generate_html_email(parsed, episode_no)
    subject    = f" {episode_no} | {parsed.get('title', episode_no)}"
    send_email(email_html, subject)

    # 更新網站
    print("🎨 產生網站頁面...")
    write_site_assets()
    page_path = build_episode_page(parsed)
    build_index()
    print(f"📄 集數頁面：{page_path}")
    print(f"🏠 首頁：{os.path.join(SITE_DIR, 'index.html')}")

    # 更新歷史
    with open(HISTORY_FILE, "w") as f:
        f.write(episode_no)
    print(f"🎉 {episode_no} 全部完成！")


if __name__ == "__main__":
    main()

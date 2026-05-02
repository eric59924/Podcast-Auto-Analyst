import os
import re
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
# ⚙️ 系統與密碼設定區 (從環境變數讀取，確保安全)
# =========================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SENDER_EMAIL = os.environ.get("GMAIL_USER")
APP_PASSWORD = os.environ.get("GMAIL_PASS")
RECEIVER_EMAIL = SENDER_EMAIL  # 預設寄給自己，或換成其他信箱

client = genai.Client(api_key=GEMINI_API_KEY)
RSS_URL = "https://open.firstory.me/rss/user/ck7t2i2ncqopi0873tqaimwq1"

# GitHub Actions 暫存環境
SAVE_DIR = "output"
os.makedirs(SAVE_DIR, exist_ok=True)
HISTORY_FILE = "last_ep.txt" # 用來記憶上次處理了哪一集

UNRESTRICTED_SAFETY = [
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
]

# =========================
# 📡 模組 1：抓取最新 RSS
# =========================
def get_latest_episode_from_rss(rss_url):
    print("📡 正在檢查 Podcast RSS 更新...")
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    response = requests.get(rss_url, headers=headers, timeout=15)
    if response.status_code != 200:
        raise Exception(f"RSS 抓取失敗，狀態碼: {response.status_code}")
        
    root = ET.fromstring(response.content)
    latest_item = root.find('.//channel/item')
    title = latest_item.find('title').text
    mp3_url = latest_item.find('enclosure').attrib['url']
    
    match = re.search(r"(EP\d+)", title, re.IGNORECASE)
    episode_no = match.group(1).upper() if match else "LATEST_EP"
    
    return episode_no, title, mp3_url

# =========================
# 📝 模組 2 & 3：語音轉文字與 AI 分析 (你的完美設定)
# =========================
def get_full_transcript(mp3_path):
    print(f"☁️ 正在上傳音檔至 Google 伺服器...")
    audio_file = client.files.upload(file=mp3_path)
    while audio_file.state.name == "PROCESSING":
        time.sleep(5)
        audio_file = client.files.get(name=audio_file.name)
    if audio_file.state.name == "FAILED":
        raise ValueError("❌ 音檔處理失敗！")
    print("✍️ 開始產生逐字稿...")
    prompt = "請根據這段音檔，整理出一份完整的繁體中文逐字稿。要求：盡可能一字不漏，加上標點符號與適當分段。請務必加上時間戳記，格式為「[分:秒] 文字」。不需做總結或摘要。"
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[audio_file, prompt],
        config=types.GenerateContentConfig(safety_settings=UNRESTRICTED_SAFETY)
    )
    client.files.delete(name=audio_file.name)
    return response.text.strip()

def analyze_from_transcript(transcript_text):
    print("🧠 正在根據逐字稿進行結構化分析...")
    prompt = f"""
    你是專業的股市投資人。
    請根據以下逐字稿進行分析，用繁體中文輸出，只輸出純 JSON，不含任何說明文字或 markdown 符號。

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
    - 從該集podcast的上船日期、或是由主持人提及的日期、新聞事件時間點推斷
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
        （例：他慣用「我覺得」「說實在」「這個角度來看」等語氣）

    ■ stocks（個股卡片）
    - 只收錄主持人明確給出投資論點的個股，純路過提及不算
    - ticker：台股填數字代號（如 2330），美股填英文（如 INTC）
        ⚠️ 台積電 = 2330，聯發科 = 2454，絕對不能填 null
        若真的不知道代號才填 null
    - market 只能選：台股半導體 / 美股半導體 / 台股網通 / 台股其他 / 美股其他
    - summary：150字以內，包含：
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
        ✅ 「主動型ETF在台股真的能長期打贏大盤嗎？」
        ❌ 「巴菲特的實驗早就證明了主動型基金長期很難打不過大盤...（落落長）」
    - points：3-4個重點，每個 label 4字以內（像小標題）
    - quote：主持人最精華金句，原文照錄
        必須是有哲理、有衝擊感、能獨立成句的話
        ✅ 「會傷到你的人，都是你最相信、最愛的人」
        ❌ 「你要長期的贏大盤真的是一個很不容易的事情」（太平淡）

    ━━━━━━━━━━━━━━━━━━━━━━━━
    【STEP 3｜Q&A 過濾標準（嚴格執行）】
    ━━━━━━━━━━━━━━━━━━━━━━━━
    ✅ 納入：
    - 選股、持股、賣股時機的具體問題
    - 特定個股或產業的看法
    - 投資心法、倉位管理、停損停利
    - 槓桿操作、維持率等實務操作
    - 總體經濟判斷與趨勢判讀
    - 主持人分享自己的投資操作或持倉

    ❌ 跳過（直接不輸出，不留空殼）：
    - 純生活問題（感情、旅遊、健康）
    - 只是問主持人個人意見但與投資無關
    - 問題本身不明確，主持人也沒給出有價值的投資觀點
    - 上市/櫃選擇這類公司治理問題，若主持人沒給投資建議也跳過

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
        "ticker": "2330 或 INTC，絕對不能因為懶得查就填null",
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
    return response.text.strip()

# =========================
# 💌 模組 4：HTML 渲染與寄信
# =========================
def generate_html_email(data):
    # 請把上一步驟修改好的「客觀版 HTML 渲染」程式碼完整貼進來
    # 包含移除了時間戳記、改為「市場快訊」的那個版本
    date_str = data.get('date') if data.get('date') else "最新市場快訊"
    title_str = data.get('title') if data.get('title') else "市場重點整理"
    html = f"""
    <html>
    <body style="font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; background-color: #f4f7f9; padding: 20px; color: #333;">
        <div style="max-width: 650px; margin: auto; background: white; padding: 30px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.05);">
            <div style="border-bottom: 2px solid #eee; padding-bottom: 15px; margin-bottom: 20px;">
                <span style="background-color: #3498db; color: white; padding: 4px 10px; border-radius: 4px; font-weight: bold; font-size: 14px;">{date_str}</span>
                <h2 style="margin: 10px 0 5px 0; color: #2c3e50;">{title_str}</h2>
            </div>
            <div style="background-color: #f8f9fa; border-left: 4px solid #f39c12; padding: 15px; border-radius: 4px; margin-bottom: 25px;">
                <h4 style="margin: 0 0 10px 0; color: #f39c12;">💡 核心摘要</h4>
                <p style="margin: 0; line-height: 1.6; font-size: 15px; color: #555;">{data.get('intro', '')}</p>
            </div>
    """
    
    market = data.get('market_view', {})
    mkt_sentiment = market.get('sentiment', '中性')
    mkt_color = "#27ae60" if mkt_sentiment == "看多" else "#c0392b" if mkt_sentiment == "看空" else "#7f8c8d"
    html += f"""
            <h3 style="color: #2c3e50; border-bottom: 1px solid #eee; padding-bottom: 5px;">📊 大盤觀點</h3>
            <p style="font-size: 16px;"><span style="color: {mkt_color}; font-weight: bold;">[{mkt_sentiment}]</span> {market.get('summary', '無特別觀點')}</p>
    """

    html += """<h3 style="color: #2c3e50; border-bottom: 1px solid #eee; padding-bottom: 5px; margin-top: 30px;">📰 市場事件與解析</h3>"""
    for news in data.get('news', []):
        html += f"""
            <div style="margin-bottom: 20px;">
                <h4 style="margin: 0 0 5px 0;">📌 {news.get('title', '')} <span style="font-size: 12px; font-weight: normal; background: #eee; padding: 2px 6px; border-radius: 4px; margin-left: 5px;">{news.get('category', '')}</span></h4>
                <p style="margin: 0 0 5px 0; font-size: 14px; color: #666;"><b>事件：</b>{news.get('event', '')}</p>
                <p style="margin: 0; font-size: 14px; color: #2c3e50;"><b>深度解析：</b>{news.get('view', '')}</p>
            </div>
        """

    html += """<h3 style="color: #2c3e50; border-bottom: 1px solid #eee; padding-bottom: 5px; margin-top: 30px;">📈 標的動態</h3>"""
    for stock in data.get('stocks', []):
        sentiment = stock.get('sentiment', '觀望')
        s_color = "#27ae60" if sentiment == "看多" else "#c0392b" if sentiment == "看空" else "#f39c12"
        ticker_str = f" ({stock.get('ticker')})" if stock.get('ticker') else ""
        catalyst = stock.get('catalyst_short') or "無"
        risk_note = stock.get('risk_note') or "無"
        risk_level = stock.get('risk') or "未知"

        html += f"""
            <div style="border: 1px solid #e0e0e0; padding: 15px; border-radius: 8px; margin-bottom: 15px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                    <b style="font-size: 16px; color: #2980b9;">{stock.get('name', '')}{ticker_str}</b>
                    <span style="background-color: {s_color}; color: white; padding: 3px 8px; border-radius: 12px; font-size: 12px; font-weight: bold;">{sentiment}</span>
                </div>
                <div style="display: flex; flex-wrap: wrap; gap: 10px; font-size: 13px; margin-bottom: 10px; color: #7f8c8d;">
                    <span style="background: #f1f2f6; padding: 2px 6px; border-radius: 4px;">⚠️ 風險: {risk_level}</span>
                    <span style="background: #f1f2f6; padding: 2px 6px; border-radius: 4px;">🚀 催化劑: {catalyst}</span>
                    <span style="background: #f1f2f6; padding: 2px 6px; border-radius: 4px;">🛡️ 風險點: {risk_note}</span>
                </div>
                <p style="margin: 0; font-size: 14px; line-height: 1.5; color: #444;">{stock.get('summary', '')}</p>
            </div>
        """

    html += """<h3 style="color: #2c3e50; border-bottom: 1px solid #eee; padding-bottom: 5px; margin-top: 30px;">💼 投資策略與實務探討</h3>"""
    for qa in data.get('qa', []):
        html += f"""
            <div style="margin-bottom: 25px;">
                <h4 style="margin: 0 0 5px 0; color: #8e44ad;">💬 {qa.get('title', '')}</h4>
                <p style="margin: 0 0 8px 0; font-size: 14px; color: #666; font-style: italic;">探討議題: {qa.get('question', '')}</p>
        """
        for pt in qa.get('points', []):
            html += f"<p style='margin: 0 0 5px 15px; font-size: 14px; line-height: 1.4;'>• <b style='color: #2c3e50;'>{pt.get('label', '')}:</b> {pt.get('content', '')}</p>"
        html += f"""
                <div style="margin-top: 10px; padding: 10px; background: #fdf2e9; border-radius: 4px; text-align: center;"><b style="color: #d35400; font-size: 15px;">「{qa.get('quote', '')}」</b></div>
            </div>
        """
    html += "</div></body></html>"
    return html

def send_email(html_content, subject_title):
    print("📧 正在連線至 Gmail 伺服器寄信...")
    msg = MIMEMultipart()
    msg['Subject'] = f"🚀 市場快訊整理：{subject_title}"
    msg['From'] = SENDER_EMAIL
    msg['To'] = RECEIVER_EMAIL
    msg.attach(MIMEText(html_content, 'html'))
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(SENDER_EMAIL, APP_PASSWORD)
            server.send_message(msg)
        print("✅ 信件發送成功！")
    except Exception as e:
        print(f"❌ 信件發送失敗：{e}")

# =========================
# 🚀 主流水線
# =========================
def main():
    print("🔥 啟動全自動分析流水線...")
    try:
        # 1. 抓取最新 EP
        episode_no, title, mp3_url = get_latest_episode_from_rss(RSS_URL)
        
        # 2. 檢查是否已經寄過這集
        last_ep = ""
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                last_ep = f.read().strip()
                
        if episode_no == last_ep:
            print(f"⚡ {episode_no} 已經處理並寄送過了，今日提早下班！")
            return

        # 3. 下載音檔
        mp3_path = os.path.join(SAVE_DIR, f"{episode_no}.mp3")
        print(f"⬇️ 開始下載 {episode_no}.mp3 ...")
        r = requests.get(mp3_url, stream=True)
        with open(mp3_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
                
        # 4. 逐字稿與分析
        transcript = get_full_transcript(mp3_path)
        if len(transcript) < 100:
            raise ValueError("❌ AI 回傳的逐字稿太短或為空！")
            
        analysis_result = analyze_from_transcript(transcript)
        parsed_data = json.loads(analysis_result)

        # 5. 寄送 Email
        print("🎨 正在打包並寄送 Email...")
        email_html = generate_html_email(parsed_data)
        send_email(email_html, parsed_data.get('title', episode_no))

        # 6. 更新歷史紀錄，防止重複寄信
        with open(HISTORY_FILE, "w") as f:
            f.write(episode_no)
        print(f"🎉 任務圓滿完成！已將 {episode_no} 標記為完成。")

    except Exception as e:
        print(f"❌ 發生錯誤：{e}")

if __name__ == "__main__":
    main()
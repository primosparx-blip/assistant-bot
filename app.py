"""
Primo — Personal Business Assistant
====================================
A fully featured Telegram bot for George Solomon, Trinidad.
Features:
- Conversation memory (per session)
- Gmail: read, search, find attachments, send files to Telegram
- Google Calendar: read events, create events, reminders
- Accounting: query all agent bots via API
- Daily 8am briefing with business lesson
- Auto email receipt scanning every 24hrs
- Proactive event reminders
- Meal and activity suggestions
- Natural language understanding via Claude
"""

import os, json, base64, datetime, hashlib, requests, traceback, threading, time, io
from flask import Flask, request
import anthropic
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

app = Flask(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN     = os.environ["ASSISTANT_BOT_TOKEN"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_TOKEN_JSON  = os.environ.get("GOOGLE_TOKEN_JSON", "")
ACCOUNTING_API_URL = os.environ.get("ACCOUNTING_API_URL", "")
OWNER_CHAT_ID      = os.environ.get("OWNER_CHAT_ID", "")
OWNER_NAME         = os.environ.get("OWNER_NAME", "George")
OWNER_EMAIL        = os.environ.get("OWNER_EMAIL", "georgejgsolomon@gmail.com")
TELEGRAM_API       = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar"
]

SYSTEM_PROMPT = """You are Primo, the personal business assistant of George Solomon, a restaurant owner in Trinidad and Tobago.

Your personality: Sharp, warm, proactive, direct. You speak like a trusted advisor — not a chatbot. You give real answers, not deflections.

What you know about George:
- Owns a restaurant business in Trinidad
- Banks with JMMB (gets transaction alerts from transactionalerts@jmmb.com)
- Uses TT RideShare for transport
- Regular suppliers: Hadco, Trinidad Seafoods, A.S. Bryden, MoreVino/MoreSushi
- Gmail: georgejgsolomon@gmail.com
- Uses TTD (Trinidad dollar) as primary currency

Your capabilities:
- Read and search Gmail
- Find and send email attachments to Telegram
- Read and create Google Calendar events
- Query accounting data (invoices, expenses, categories)
- Search the web for current information
- Remember the full conversation history

Rules:
- NEVER say you don't have access to data — you do, use it
- Always be specific with names, amounts, dates
- Flag anything unusual or worth noting proactively
- Use TTD for local currency, note when amounts are USD
- Keep responses conversational, not bullet-pointed unless it helps
- If you need to do something, say what you're doing"""

# ── Conversation Memory ──────────────────────────────────────────────────────
conversation_history = {}

def get_history(chat_id):
    return conversation_history.get(str(chat_id), [])

def add_to_history(chat_id, role, content):
    key = str(chat_id)
    if key not in conversation_history:
        conversation_history[key] = []
    conversation_history[key].append({"role": role, "content": content[:2000]})
    if len(conversation_history[key]) > 20:
        conversation_history[key] = conversation_history[key][-20:]

def clear_history(chat_id):
    conversation_history[str(chat_id)] = []

# ── Google Auth ──────────────────────────────────────────────────────────────
def get_google_creds():
    if GOOGLE_TOKEN_JSON:
        token_data = json.loads(GOOGLE_TOKEN_JSON)
    else:
        with open("token.json") as f:
            token_data = json.load(f)
    creds = Credentials.from_authorized_user_info(token_data, SCOPES)
    try:
        if not creds.valid or creds.expired:
            creds.refresh(Request())
    except Exception:
        creds.refresh(Request())
    return creds

def get_gmail():
    return build("gmail", "v1", credentials=get_google_creds())

def get_calendar():
    return build("calendar", "v3", credentials=get_google_creds())

# ── Gmail Functions ──────────────────────────────────────────────────────────
def search_emails(query, max_results=10):
    svc  = get_gmail()
    res  = svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    msgs = res.get("messages", [])
    results = []
    for m in msgs:
        d = svc.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From","Subject","Date"]
        ).execute()
        h = {x["name"]:x["value"] for x in d["payload"]["headers"]}
        results.append({
            "id":      m["id"],
            "from":    h.get("From",""),
            "subject": h.get("Subject",""),
            "date":    h.get("Date",""),
            "snippet": d.get("snippet","")[:150]
        })
    return results

def get_email_body_and_attachments(msg_id):
    svc     = get_gmail()
    detail  = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
    payload = detail.get("payload", {})

    def extract_text(part):
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
        for sub in part.get("parts", []):
            result = extract_text(sub)
            if result:
                return result
        return ""

    def extract_attachments(part, atts=None):
        if atts is None:
            atts = []
        if part.get("filename"):
            atts.append({
                "filename":      part["filename"],
                "mimeType":      part.get("mimeType",""),
                "attachment_id": part.get("body",{}).get("attachmentId",""),
                "size":          part.get("body",{}).get("size",0)
            })
        for sub in part.get("parts", []):
            extract_attachments(sub, atts)
        return atts

    body        = extract_text(payload)[:3000]
    attachments = extract_attachments(payload)
    return body, attachments

def download_and_send_attachment(chat_id, msg_id, attachment_id, filename, caption=""):
    svc = get_gmail()
    att = svc.users().messages().attachments().get(
        userId="me", messageId=msg_id, id=attachment_id
    ).execute()
    file_data  = att.get("data","")
    file_bytes = base64.urlsafe_b64decode(file_data + "==")
    files = {"document": (filename, io.BytesIO(file_bytes))}
    data  = {"chat_id": chat_id, "caption": caption[:200]}
    r = requests.post(TELEGRAM_API + "/sendDocument", data=data, files=files, timeout=30)
    return r.json().get("ok", False)

def send_gmail(to, subject, body):
    svc     = get_gmail()
    message = MIMEText(body)
    message["to"]      = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()

# ── Calendar Functions ───────────────────────────────────────────────────────
def get_events(days_ahead=1, days_back=0):
    svc   = get_calendar()
    now   = datetime.datetime.utcnow()
    start = (now - datetime.timedelta(days=days_back)).isoformat() + "Z"
    end   = (now + datetime.timedelta(days=days_ahead)).isoformat() + "Z"
    res   = svc.events().list(
        calendarId="primary", timeMin=start, timeMax=end,
        singleEvents=True, orderBy="startTime", maxResults=20
    ).execute()
    return res.get("items", [])

def format_event(event):
    start    = event.get("start", {})
    time_str = start.get("dateTime","") or start.get("date","")
    try:
        dt       = datetime.datetime.fromisoformat(time_str.replace("Z",""))
        dt_local = dt - datetime.timedelta(hours=4)
        time_str = dt_local.strftime("%a %b %d, %I:%M %p")
    except:
        pass
    return event.get("summary","No title") + " — " + time_str

def create_event(summary, start_dt, end_dt, description=""):
    svc   = get_calendar()
    event = {
        "summary":     summary,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/Port_of_Spain"},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "America/Port_of_Spain"},
    }
    return svc.events().insert(calendarId="primary", body=event).execute()

# ── Accounting Bot API ───────────────────────────────────────────────────────
def get_accounting_summary():
    if not ACCOUNTING_API_URL:
        return {}
    try:
        r = requests.get(ACCOUNTING_API_URL + "/api/summary", timeout=10)
        return r.json()
    except:
        return {}

def get_all_invoices(limit=50):
    if not ACCOUNTING_API_URL:
        return []
    try:
        r = requests.get(ACCOUNTING_API_URL + "/api/invoices?limit=" + str(limit), timeout=10)
        return r.json().get("invoices", [])
    except:
        return []

def log_invoice_to_sheet(data, source="email_scan"):
    if not ACCOUNTING_API_URL:
        return None
    try:
        r = requests.post(
            ACCOUNTING_API_URL + "/api/log_invoice",
            json={"data": data, "source": source},
            timeout=15
        )
        return r.json().get("inv_id")
    except:
        return None

# ── Web Search ───────────────────────────────────────────────────────────────
def web_search(query):
    try:
        r = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            timeout=10
        )
        data    = r.json()
        results = []
        if data.get("AbstractText"):
            results.append(data["AbstractText"])
        for topic in data.get("RelatedTopics", [])[:3]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append(topic["Text"])
        return " | ".join(results[:3]) if results else "No results found for: " + query
    except Exception as e:
        return "Search error: " + str(e)

# ── Daily Business Lesson ────────────────────────────────────────────────────
CONCEPTS = [
    "opportunity cost","cash flow","gross margin","EBITDA","working capital",
    "accounts receivable","break even point","return on investment",
    "cost of goods sold","net profit margin","liquidity","economies of scale",
    "fixed vs variable costs","price elasticity","brand equity",
    "customer lifetime value","churn rate","gross profit","overheads",
    "markup vs margin","debtors and creditors","accounts payable",
    "depreciation","inventory turnover","profit margin"
]

def get_daily_lesson():
    today   = datetime.date.today().isoformat()
    idx     = int(hashlib.md5(today.encode()).hexdigest(), 16) % len(CONCEPTS)
    concept = CONCEPTS[idx]
    resp    = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=120,
        messages=[{"role":"user","content":
            "Explain " + concept + " in 2-3 simple sentences with a practical example "
            "a restaurant owner in Trinidad would relate to. Under 60 words. No formatting."
        }]
    )
    return "Today's Business Lesson: " + concept.title() + "\n\n" + resp.content[0].text

# ── Build Context for Claude ─────────────────────────────────────────────────
def build_context(text):
    text_l  = text.lower()
    context = ""

    # Always fetch accounting data
    try:
        summary  = get_accounting_summary()
        invoices = get_all_invoices(20)
        if summary.get("status") == "ok":
            inv_lines = []
            for inv in invoices[-10:]:
                inv_lines.append(
                    str(inv.get("ID","")) + "|" + str(inv.get("Date Received","")) + "|" +
                    str(inv.get("Vendor","")) + "|" + str(inv.get("Category","")) + "|TTD " +
                    str(inv.get("Total",0)) + "|" + str(inv.get("Status",""))
                )
            context += (
                "\n\n[ACCOUNTING] " +
                "Total invoices: " + str(summary.get("total_invoices",0)) +
                ", This week: TTD " + str(round(summary.get("total_spend_this_week",0),2)) +
                ", This month: TTD " + str(round(summary.get("total_spend_this_month",0),2)) +
                "\nRecent invoices: " + " || ".join(inv_lines)
            )
    except Exception as e:
        context += "\n[ACCOUNTING] Unavailable: " + str(e)[:50]

    # Fetch emails — always, for any question
    try:
        # Build smart query
        q = "in:inbox newer_than:7d"
        skip = {"what","is","my","the","from","have","any","did","get","for","that","this",
                "your","can","you","see","email","inbox","mail","about","were","there",
                "emails","messages","yesterday","today","week","any","are","there","was"}
        for word in text.split():
            w = word.strip("?.,!").lower()
            if len(w) > 3 and w not in skip:
                q = "in:inbox newer_than:14d " + word.strip("?.,!")
                break
        if "yesterday" in text_l:
            q = "in:inbox newer_than:2d older_than:1d"
        elif "today" in text_l:
            q = "in:inbox newer_than:1d"
        elif "last week" in text_l or "this week" in text_l:
            q = "in:inbox newer_than:7d"
        elif "jmmb" in text_l or "transaction" in text_l:
            q = "from:transactionalerts@jmmb.com newer_than:7d"

        emails = search_emails(q, max_results=8)
        if emails:
            lines = []
            for em in emails:
                sf = em["from"][:35].replace('"',"")
                ss = em["subject"][:50].replace('"',"")
                sp = em["snippet"][:80].replace('"',"")
                lines.append("From:" + sf + " Subj:" + ss + " Preview:" + sp)
            context += "\n\n[EMAILS] " + " || ".join(lines)
        else:
            context += "\n\n[EMAILS] No emails found for that search."
    except Exception as e:
        context += "\n\n[EMAILS] Unavailable: " + str(e)[:80]

    # Fetch calendar if relevant
    cal_kws = ["calendar","schedule","meeting","today","tomorrow","week","event","appointment","reminder","upcoming"]
    if any(w in text_l for w in cal_kws):
        try:
            events = get_events(days_ahead=7)
            if events:
                ev_lines = [format_event(e) for e in events[:8]]
                context += "\n\n[CALENDAR] Upcoming: " + " | ".join(ev_lines)
            else:
                context += "\n\n[CALENDAR] No upcoming events."
        except Exception as e:
            context += "\n\n[CALENDAR] Unavailable: " + str(e)[:50]

    return context

# ── Telegram Helpers ─────────────────────────────────────────────────────────
def send_message(chat_id, text, parse_mode=""):
    # Split long messages
    if len(text) > 4000:
        chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for chunk in chunks:
            requests.post(TELEGRAM_API + "/sendMessage",
                json={"chat_id": chat_id, "text": chunk, "parse_mode": parse_mode})
            time.sleep(0.3)
    else:
        requests.post(TELEGRAM_API + "/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode})

def get_file_url(file_id):
    r    = requests.get(TELEGRAM_API + "/getFile", params={"file_id": file_id})
    path = r.json()["result"]["file_path"]
    return "https://api.telegram.org/file/bot" + TELEGRAM_TOKEN + "/" + path

# ── Morning Briefing ─────────────────────────────────────────────────────────
def build_morning_briefing():
    today    = datetime.datetime.utcnow() - datetime.timedelta(hours=4)
    day_str  = today.strftime("%A, %B %d, %Y")
    hour     = today.hour
    greeting = "Good morning" if hour < 12 else ("Good afternoon" if hour < 17 else "Good evening")
    lines    = [greeting + ", " + OWNER_NAME + "!", day_str, ""]

    # Calendar
    try:
        events = get_events(days_ahead=1)
        if events:
            lines.append("Todays Schedule (" + str(len(events)) + " events):")
            for e in events:
                lines.append("  " + format_event(e))
        else:
            lines.append("Calendar: Nothing scheduled today.")
    except:
        lines.append("Calendar: unavailable")

    lines.append("")

    # Email scan for overnight receipts
    try:
        receipt_emails = search_emails(
            "from:(transactionalerts@jmmb.com OR ttrideshare OR noreply) newer_than:1d",
            max_results=10
        )
        if receipt_emails:
            lines.append("Overnight Receipts (" + str(len(receipt_emails)) + " found):")
            for em in receipt_emails[:5]:
                lines.append("  " + em["from"][:30] + " — " + em["subject"][:40])
        else:
            lines.append("Receipts: None overnight.")
    except:
        lines.append("Receipts: unavailable")

    lines.append("")

    # Accounting summary
    try:
        summary = get_accounting_summary()
        if summary.get("status") == "ok":
            lines.append("Expenses:")
            lines.append("  This week: TTD " + str(round(summary.get("total_spend_this_week",0),2)))
            lines.append("  This month: TTD " + str(round(summary.get("total_spend_this_month",0),2)))
            lines.append("  Pending: " + str(summary.get("pending_invoices",0)) + " invoices")
    except:
        lines.append("Expenses: unavailable")

    lines.append("")

    # Upcoming events next 3 days
    try:
        upcoming = get_events(days_ahead=3)
        future   = [e for e in upcoming if e.get("start",{}).get("dateTime","") > datetime.datetime.utcnow().isoformat()]
        if future:
            lines.append("Coming Up (next 3 days):")
            for e in future[:3]:
                lines.append("  " + format_event(e))
    except:
        pass

    lines.append("")
    lines.append("---")

    # Daily lesson
    try:
        lines.append(get_daily_lesson())
    except:
        pass

    lines.append("")
    lines.append("Type /help for all commands")
    return "\n".join(lines)

# ── Schedulers ───────────────────────────────────────────────────────────────
def morning_scheduler():
    last_sent = None
    while True:
        now  = datetime.datetime.utcnow()
        date = now.date()
        if now.hour == 12 and now.minute < 5 and OWNER_CHAT_ID and last_sent != date:
            try:
                briefing = build_morning_briefing()
                send_message(OWNER_CHAT_ID, briefing)
                last_sent = date
                print("Morning briefing sent at " + str(now))
            except Exception as e:
                print("Briefing error: " + str(e))
        time.sleep(60)

def event_reminder_scheduler():
    """Check for events starting in 30 minutes and send reminders."""
    while True:
        try:
            if OWNER_CHAT_ID:
                now      = datetime.datetime.utcnow()
                soon     = now + datetime.timedelta(minutes=35)
                events   = get_events(days_ahead=0)
                for event in events:
                    start_str = event.get("start",{}).get("dateTime","")
                    if not start_str:
                        continue
                    try:
                        start_dt = datetime.datetime.fromisoformat(start_str.replace("Z",""))
                        diff     = (start_dt - now).total_seconds() / 60
                        if 25 <= diff <= 35:
                            send_message(OWNER_CHAT_ID,
                                "Reminder: " + event.get("summary","Event") +
                                " starts in 30 minutes!"
                            )
                    except:
                        pass
        except:
            pass
        time.sleep(300)  # check every 5 minutes

# ── Main Webhook ─────────────────────────────────────────────────────────────
@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    update  = request.json
    if not update:
        return "ok"
    msg     = update.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    text    = msg.get("text", "").strip()
    text_l  = text.lower()
    photo   = msg.get("photo")
    document= msg.get("document")

    if not chat_id:
        return "ok"

    try:

        # ── /start or /help ───────────────────────────────────────────────
        if text_l in ("/start","/help","help"):
            send_message(chat_id,
                "Hey " + OWNER_NAME + "! I'm Primo, your personal business assistant.\n\n"
                "I can help you with:\n\n"
                "EMAILS\n"
                "- Read and search your inbox\n"
                "- Find and send you attachments\n"
                "- Send emails on your behalf\n\n"
                "CALENDAR\n"
                "- Check your schedule\n"
                "- Create events\n"
                "- 30-min reminders before events\n\n"
                "ACCOUNTING\n"
                "- Invoice and expense queries\n"
                "- Spending summaries\n\n"
                "DAILY BRIEFING\n"
                "- Sent every morning at 8am\n"
                "- Or type /briefing anytime\n\n"
                "Just talk to me naturally — I understand plain English!\n\n"
                "Commands: /briefing /emails /today /week /help /clearmemory"
            )
            return "ok"

        # ── Clear conversation memory ─────────────────────────────────────
        if text_l in ("/clearmemory","clear memory","forget","start over","new conversation"):
            clear_history(chat_id)
            send_message(chat_id, "Memory cleared! Starting fresh.")
            return "ok"

        # ── Morning briefing ──────────────────────────────────────────────
        if text_l in ("/briefing","briefing","morning","good morning","daily update","update"):
            send_message(chat_id, "Getting your briefing...")
            def send_briefing():
                briefing = build_morning_briefing()
                send_message(chat_id, briefing)
            threading.Thread(target=send_briefing, daemon=True).start()
            return "ok"

        # ── Calendar: today ───────────────────────────────────────────────
        if text_l in ("/today","today","whats today","what's today","schedule today"):
            events = get_events(days_ahead=1)
            if not events:
                send_message(chat_id, "Nothing on your calendar today — free day!")
            else:
                lines = [datetime.date.today().strftime("%A, %B %d") + "\n"]
                for e in events:
                    lines.append(format_event(e))
                send_message(chat_id, "\n".join(lines))
            return "ok"

        # ── Calendar: week ────────────────────────────────────────────────
        if text_l in ("/week","this week","weekly schedule","week ahead"):
            events = get_events(days_ahead=7)
            if not events:
                send_message(chat_id, "Nothing in your calendar this week!")
            else:
                lines = ["This Week\n"]
                for e in events[:10]:
                    lines.append(format_event(e))
                send_message(chat_id, "\n".join(lines))
            return "ok"

        # ── Emails ────────────────────────────────────────────────────────
        if text_l in ("/emails","emails","check email","inbox","unread","new emails"):
            def fetch_emails():
                try:
                    emails = search_emails("in:inbox is:unread newer_than:3d", max_results=8)
                    if not emails:
                        send_message(chat_id, "Your inbox is clear — no unread emails!")
                        return
                    lines = [str(len(emails)) + " unread emails:\n"]
                    for i, em in enumerate(emails, 1):
                        lines.append(str(i) + ". " + em["subject"][:50])
                        lines.append("   From: " + em["from"][:40])
                        lines.append("   " + em["snippet"][:80])
                        lines.append("")
                    send_message(chat_id, "\n".join(lines))
                except Exception as e:
                    send_message(chat_id, "Email error: " + str(e)[:100])
            send_message(chat_id, "Checking your inbox...")
            threading.Thread(target=fetch_emails, daemon=True).start()
            return "ok"

        # ── Attachment request ────────────────────────────────────────────
        attach_kws = ["attachment","pdf","document","report","file","spreadsheet","send me","get the","retrieve","download","grab"]
        if any(w in text_l for w in attach_kws):
            def fetch_attachments():
                try:
                    skip = {"attachment","file","pdf","document","report","email","please","from",
                            "with","the","and","get","pull","grab","retrieve","download","send","me",
                            "my","can","you","have","that","this","is","in","there","any"}
                    hint = ""
                    for word in text.split():
                        w = word.strip("?.,!").lower()
                        if len(w) > 3 and w not in skip:
                            hint = word.strip("?.,!")
                            break

                    q = "in:inbox has:attachment newer_than:14d"
                    if hint:
                        q += " " + hint

                    send_message(chat_id, "Searching for attachments" + (" matching '" + hint + "'" if hint else "") + "...")
                    emails = search_emails(q, max_results=5)

                    if not emails:
                        send_message(chat_id, "No emails with attachments found" + (" for '" + hint + "'" if hint else "") + ".")
                        return

                    svc        = get_gmail()
                    files_sent = 0
                    for em in emails[:3]:
                        body, atts = get_email_body_and_attachments(em["id"])
                        if atts:
                            send_message(chat_id,
                                "Found in: " + em["subject"][:50] +
                                "\nFrom: " + em["from"][:40] +
                                "\nSending " + str(len(atts)) + " file(s)..."
                            )
                            for att in atts[:3]:
                                if att.get("attachment_id"):
                                    ok = download_and_send_attachment(
                                        chat_id, em["id"],
                                        att["attachment_id"],
                                        att["filename"],
                                        "From: " + em["from"][:30]
                                    )
                                    if ok:
                                        files_sent += 1

                    if files_sent == 0:
                        send_message(chat_id, "Found emails but couldn't download the files. Try asking the sender to resend.")
                    else:
                        send_message(chat_id, "Sent " + str(files_sent) + " file(s) to you!")
                except Exception as e:
                    send_message(chat_id, "Error fetching attachment: " + str(e)[:100])

            threading.Thread(target=fetch_attachments, daemon=True).start()
            return "ok"

        # ── Scan emails for receipts ──────────────────────────────────────
        scan_kws = ["scan emails","check emails for receipts","scan for receipts","find receipts","scan inbox","/scanemails"]
        if any(p in text_l for p in scan_kws):
            days = 10
            for word in text_l.split():
                if word.isdigit():
                    days = int(word)
                    break
            send_message(chat_id, "Scanning emails for receipts from the last " + str(days) + " days...")
            def do_scan():
                try:
                    q = (
                        "in:inbox newer_than:" + str(days) + "d "
                        "(from:transactionalerts@jmmb.com OR from:ttrideshare OR "
                        "subject:receipt OR subject:invoice OR subject:payment OR "
                        "subject:transaction OR subject:summary OR subject:confirmation)"
                    )
                    emails = search_emails(q, max_results=20)
                    if not emails:
                        send_message(chat_id, "No receipts found in the last " + str(days) + " days.")
                        return
                    logged = []
                    for em in emails:
                        body, _ = get_email_body_and_attachments(em["id"])
                        prompt  = (
                            "Extract receipt/invoice data from: "
                            "From:" + em["from"] + " Subject:" + em["subject"] +
                            " Body:" + body[:1500] +
                            " Return JSON: {vendor,invoice_date,invoice_number,description,category,currency,amount,tax,confidence} "
                            "or SKIP if not a receipt. Currency should be TTD for local, USD for international."
                        )
                        resp = claude.messages.create(
                            model="claude-sonnet-4-5", max_tokens=300,
                            messages=[{"role":"user","content": prompt}]
                        )
                        raw = resp.content[0].text.strip()
                        if raw.upper().startswith("SKIP"):
                            continue
                        if raw.startswith("```"):
                            raw = raw.split("```")[1]
                            if raw.startswith("json"):
                                raw = raw[4:]
                        try:
                            data   = json.loads(raw)
                            inv_id = log_invoice_to_sheet(data, "email_scan")
                            if inv_id:
                                logged.append(inv_id + " | " + data.get("vendor","?") + " | " + str(data.get("currency","TTD")) + " " + str(data.get("amount",0)))
                        except:
                            pass
                    if logged:
                        send_message(chat_id, "Logged " + str(len(logged)) + " receipt(s):\n" + "\n".join(logged) + "\n\nAll added to your Google Sheet!")
                    else:
                        send_message(chat_id, "Scanned " + str(len(emails)) + " emails but no parseable receipts found.")
                except Exception as e:
                    send_message(chat_id, "Scan error: " + str(e)[:100])
            threading.Thread(target=do_scan, daemon=True).start()
            return "ok"

        # ── Clear accounting sheet ────────────────────────────────────────
        clear_kws = ["clear the sheet","clear sheet","delete all invoices","delete all entries",
                     "start fresh","wipe the sheet","reset the sheet","clear all data"]
        if any(p in text_l for p in clear_kws):
            if ACCOUNTING_API_URL:
                r    = requests.post(ACCOUNTING_API_URL + "/api/clearsheet", timeout=15)
                data = r.json()
                if data.get("status") == "ok":
                    send_message(chat_id, "All sheets cleared! Next invoice will be INV-001.")
                else:
                    send_message(chat_id, "Error: " + data.get("message","unknown"))
            return "ok"

        # ── Web search ────────────────────────────────────────────────────
        if text_l.startswith("/search") or "search for" in text_l or "look up" in text_l:
            query = text.replace("/search","").replace("search for","").replace("look up","").strip()
            def do_search():
                result = web_search(query)
                resp   = claude.messages.create(
                    model="claude-sonnet-4-5", max_tokens=300,
                    messages=[{"role":"user","content":
                        "User searched: " + query + ". Results: " + result + ". Summarise in 3-5 sentences."
                    }]
                )
                send_message(chat_id, resp.content[0].text)
            send_message(chat_id, "Searching for: " + query + "...")
            threading.Thread(target=do_search, daemon=True).start()
            return "ok"

        # ── Photo/document received ───────────────────────────────────────
        if photo or (document and document.get("mime_type","").startswith("image")):
            caption = msg.get("caption","").lower()
            send_message(chat_id, "Got it! Reading your invoice...")
            def process_image():
                try:
                    file_id   = photo[-1]["file_id"] if photo else document["file_id"]
                    file_url  = get_file_url(file_id)
                    img_data  = requests.get(file_url).content
                    image_b64 = base64.standard_b64encode(img_data).decode("utf-8")
                    resp = claude.messages.create(
                        model="claude-sonnet-4-5", max_tokens=400,
                        messages=[{"role":"user","content":[
                            {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":image_b64}},
                            {"type":"text","text":
                                "Extract invoice data. Return JSON: "
                                "{vendor,invoice_date,invoice_number,description,category,currency,amount,tax,confidence}. "
                                "Category options: Office Supplies|Software/Cloud|Shipping|Facilities|Marketing|Travel|Utilities|Professional Services|Food & Entertainment|Other. "
                                "Default currency TTD."}
                        ]}]
                    )
                    raw = resp.content[0].text.strip()
                    if raw.startswith("```"):
                        raw = raw.split("```")[1]
                        if raw.startswith("json"): raw = raw[4:]
                    data   = json.loads(raw)
                    inv_id = log_invoice_to_sheet(data, "telegram_photo")
                    amount = float(data.get("amount",0))
                    tax    = float(data.get("tax",0))
                    send_message(chat_id,
                        "Invoice Logged!\n\n"
                        "ID: " + str(inv_id) + "\n"
                        "Vendor: " + data.get("vendor","Unknown") + "\n"
                        "Date: " + data.get("invoice_date","N/A") + "\n"
                        "Category: " + data.get("category","Other") + "\n"
                        "Amount: " + data.get("currency","TTD") + " " + str(amount) + "\n"
                        "Tax: " + data.get("currency","TTD") + " " + str(tax) + "\n"
                        "Total: " + data.get("currency","TTD") + " " + str(round(amount+tax,2)) + "\n"
                        "Sheet updated!"
                    )
                except Exception as e:
                    send_message(chat_id, "Error processing image: " + str(e)[:100])
            threading.Thread(target=process_image, daemon=True).start()
            return "ok"

        # ── Natural language handler ──────────────────────────────────────
        # Run in background thread to avoid Telegram timeout
        def handle_natural_language():
            try:
                context = build_context(text)
                history = get_history(chat_id)
                full_msg = text + context

                response = claude.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=600,
                    system=SYSTEM_PROMPT,
                    messages=history + [{"role": "user", "content": full_msg}]
                )
                reply = response.content[0].text
                add_to_history(chat_id, "user", text)
                add_to_history(chat_id, "assistant", reply)
                send_message(chat_id, reply)
            except Exception as e:
                traceback.print_exc()
                send_message(chat_id, "Error: " + str(e)[:100])

        threading.Thread(target=handle_natural_language, daemon=True).start()

    except Exception as e:
        traceback.print_exc()
        send_message(chat_id, "Something went wrong: " + str(e)[:100] + "\n\nTry /help")

    return "ok"

# ── Health Check ─────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return {"status":"ok","bot":"Primo Assistant","time":str(datetime.datetime.now())}

# ── Start Background Schedulers ───────────────────────────────────────────────
threading.Thread(target=morning_scheduler,        daemon=True).start()
threading.Thread(target=event_reminder_scheduler, daemon=True).start()

if __name__ == "__main__":
    print("Primo Assistant starting on http://localhost:5002")
    app.run(host="0.0.0.0", port=5002, debug=False)

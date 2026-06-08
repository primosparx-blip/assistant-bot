"""
Primo v2 — Tool-Use Personal Assistant
========================================
Claude is in control. Claude decides when to search emails,
check calendar, query accounting, search the web, etc.
No keyword matching. Pure conversational AI.
"""

import os, json, base64, datetime, hashlib, requests, traceback, threading, time, io, re
from flask import Flask, request
import anthropic
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from email.mime.text import MIMEText

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN     = os.environ["ASSISTANT_BOT_TOKEN"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_TOKEN_JSON  = os.environ.get("GOOGLE_TOKEN_JSON", "")
ACCOUNTING_API_URL = os.environ.get("ACCOUNTING_API_URL", "")
OWNER_CHAT_ID      = os.environ.get("OWNER_CHAT_ID", "")
OWNER_NAME         = os.environ.get("OWNER_NAME", "George")
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
- Banks with JMMB (transaction alerts from transactionalerts@jmmb.com)
- Uses TT RideShare for transport
- Regular suppliers: Hadco, Trinidad Seafoods, A.S. Bryden, MoreVino/MoreSushi
- Gmail: georgejgsolomon@gmail.com
- Primary currency: TTD (Trinidad dollars)

You have tools available. Use them proactively:
- When George asks about emails, people, or messages → use search_emails
- When George asks about his schedule, meetings, events → use get_calendar_events
- When George asks about expenses, invoices, spending → use get_accounting_data
- When George asks about files or attachments → use get_email_attachments
- When George needs something looked up online → use web_search
- When George wants to schedule something → use create_calendar_event
- When George wants to send an email → use send_email

IMPORTANT RULES:
- Always use tools to get real data before answering questions about emails/calendar/expenses
- Never say you don't have access — you have tools, use them
- Never ask George to rephrase in a specific way — understand his intent
- Be conversational and natural, like a real assistant
- Use TTD for local currency
- Remember the conversation history and maintain context"""

# ── Conversation Memory ───────────────────────────────────────────────────────
conversation_history = {}

def get_history(chat_id):
    return conversation_history.get(str(chat_id), [])

def add_to_history(chat_id, role, content):
    key = str(chat_id)
    if key not in conversation_history:
        conversation_history[key] = []
    if isinstance(content, list):
        conversation_history[key].append({"role": role, "content": content})
    else:
        conversation_history[key].append({"role": role, "content": str(content)[:3000]})
    if len(conversation_history[key]) > 20:
        conversation_history[key] = conversation_history[key][-20:]

def clear_history(chat_id):
    conversation_history[str(chat_id)] = []

# ── Google Auth ───────────────────────────────────────────────────────────────
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
        try:
            creds.refresh(Request())
        except:
            pass
    return creds

def get_gmail():
    return build("gmail", "v1", credentials=get_google_creds())

def get_calendar_svc():
    return build("calendar", "v3", credentials=get_google_creds())

# ── Tool Implementations ──────────────────────────────────────────────────────

def tool_search_emails(query, max_results=8):
    """Search Gmail and return email summaries."""
    try:
        svc  = get_gmail()
        res  = svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        msgs = res.get("messages", [])
        if not msgs:
            return {"emails": [], "count": 0, "message": "No emails found for: " + query}
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
                "snippet": d.get("snippet","")[:200]
            })
        return {"emails": results, "count": len(results)}
    except Exception as e:
        return {"error": str(e)}

def tool_read_email(email_id):
    """Read the full content of a specific email including attachments."""
    try:
        svc    = get_gmail()
        detail = svc.users().messages().get(userId="me", id=email_id, format="full").execute()
        payload = detail.get("payload", {})

        def extract_text(part):
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            if part.get("mimeType") == "text/html":
                data = part.get("body", {}).get("data", "")
                if data:
                    html = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                    text = re.sub(r"<[^>]+>", " ", html)
                    return re.sub(r"\s+", " ", text).strip()
            for sub in part.get("parts", []):
                result = extract_text(sub)
                if result:
                    return result
            return ""

        def get_attachments(part, atts=None):
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
                get_attachments(sub, atts)
            return atts

        h = {x["name"]:x["value"] for x in payload.get("headers",[])}
        body        = extract_text(payload)
        attachments = get_attachments(payload)

        return {
            "from":        h.get("From",""),
            "subject":     h.get("Subject",""),
            "date":        h.get("Date",""),
            "body":        body[:3000] if body else "(no text body — may be attachment only)",
            "attachments": attachments
        }
    except Exception as e:
        return {"error": str(e)}

def tool_send_attachment_to_telegram(chat_id, email_id, attachment_id, filename):
    """Download email attachment and send to Telegram."""
    try:
        svc        = get_gmail()
        att        = svc.users().messages().attachments().get(
            userId="me", messageId=email_id, id=attachment_id
        ).execute()
        file_data  = att.get("data","")
        file_bytes = base64.urlsafe_b64decode(file_data + "==")
        files      = {"document": (filename, io.BytesIO(file_bytes))}
        data       = {"chat_id": chat_id}
        r          = requests.post(TELEGRAM_API + "/sendDocument", data=data, files=files, timeout=30)
        result     = r.json()
        if result.get("ok"):
            return {"success": True, "message": "Sent " + filename + " to Telegram"}
        return {"success": False, "error": str(result)}
    except Exception as e:
        return {"success": False, "error": str(e)}

def tool_get_calendar_events(days_ahead=7, days_back=0):
    """Get calendar events."""
    try:
        svc   = get_calendar_svc()
        now   = datetime.datetime.utcnow()
        start = (now - datetime.timedelta(days=days_back)).isoformat() + "Z"
        end   = (now + datetime.timedelta(days=days_ahead)).isoformat() + "Z"
        res   = svc.events().list(
            calendarId="primary", timeMin=start, timeMax=end,
            singleEvents=True, orderBy="startTime", maxResults=20
        ).execute()
        events = []
        for e in res.get("items", []):
            start_dt = e.get("start",{}).get("dateTime","") or e.get("start",{}).get("date","")
            try:
                dt       = datetime.datetime.fromisoformat(start_dt.replace("Z",""))
                dt_local = dt - datetime.timedelta(hours=4)
                time_str = dt_local.strftime("%A, %B %d at %I:%M %p")
            except:
                time_str = start_dt
            events.append({
                "title":    e.get("summary","No title"),
                "time":     time_str,
                "location": e.get("location",""),
                "description": e.get("description","")
            })
        return {"events": events, "count": len(events)}
    except Exception as e:
        return {"error": str(e)}

def tool_create_calendar_event(title, date, start_time, duration_hours=1, description=""):
    """Create a calendar event."""
    try:
        svc = get_calendar_svc()
        parts = date.split("-")
        tparts = start_time.split(":")
        start_dt = datetime.datetime(int(parts[0]),int(parts[1]),int(parts[2]),
                                     int(tparts[0]),int(tparts[1]))
        end_dt   = start_dt + datetime.timedelta(hours=float(duration_hours))
        event    = {
            "summary":     title,
            "description": description,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/Port_of_Spain"},
            "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "America/Port_of_Spain"},
        }
        result = svc.events().insert(calendarId="primary", body=event).execute()
        return {"success": True, "event": title, "time": start_dt.strftime("%A, %B %d at %I:%M %p")}
    except Exception as e:
        return {"success": False, "error": str(e)}

def tool_send_email(to, subject, body):
    """Send an email."""
    try:
        svc     = get_gmail()
        message = MIMEText(body)
        message["to"]      = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        return {"success": True, "message": "Email sent to " + to}
    except Exception as e:
        return {"success": False, "error": str(e)}

def tool_get_accounting_data():
    """Get accounting summary and recent invoices."""
    if not ACCOUNTING_API_URL:
        return {"error": "Accounting API not configured"}
    try:
        sum_r  = requests.get(ACCOUNTING_API_URL + "/api/summary", timeout=10)
        inv_r  = requests.get(ACCOUNTING_API_URL + "/api/invoices?limit=20", timeout=10)
        sdata  = sum_r.json()
        idata  = inv_r.json()
        return {
            "summary":  sdata,
            "invoices": idata.get("invoices", [])
        }
    except Exception as e:
        return {"error": str(e)}

def tool_web_search(query):
    """Search the web for current information."""
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
        return {"results": " | ".join(results[:3]) if results else "No results found"}
    except Exception as e:
        return {"error": str(e)}

def tool_clear_accounting_sheet():
    """Clear all invoice data from the accounting sheet."""
    if not ACCOUNTING_API_URL:
        return {"error": "Accounting API not configured"}
    try:
        r = requests.post(ACCOUNTING_API_URL + "/api/clearsheet", timeout=15)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

# ── Tool Definitions for Claude ───────────────────────────────────────────────
TOOLS = [
    {
        "name": "search_emails",
        "description": "Search Gmail inbox. Use this whenever George asks about emails, messages, or specific people. Build a good Gmail search query (e.g. 'from:sean', 'subject:invoice', 'newer_than:7d', 'from:jmmb').",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":       {"type": "string", "description": "Gmail search query e.g. 'from:sean newer_than:7d' or 'subject:invoice newer_than:14d'"},
                "max_results": {"type": "integer", "description": "Max emails to return (default 8)", "default": 8}
            },
            "required": ["query"]
        }
    },
    {
        "name": "read_email",
        "description": "Read the full content and attachments of a specific email. Use after search_emails to get the full body of an email.",
        "input_schema": {
            "type": "object",
            "properties": {
                "email_id": {"type": "string", "description": "The email ID from search_emails results"}
            },
            "required": ["email_id"]
        }
    },
    {
        "name": "send_attachment_to_telegram",
        "description": "Download an email attachment and send it to George via Telegram. Use when George wants a file from his email.",
        "input_schema": {
            "type": "object",
            "properties": {
                "email_id":      {"type": "string", "description": "Email ID containing the attachment"},
                "attachment_id": {"type": "string", "description": "Attachment ID from read_email results"},
                "filename":      {"type": "string", "description": "Filename of the attachment"}
            },
            "required": ["email_id", "attachment_id", "filename"]
        }
    },
    {
        "name": "get_calendar_events",
        "description": "Get George's calendar events. Use when he asks about his schedule, meetings, appointments, or upcoming events.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {"type": "integer", "description": "How many days ahead to look (default 7)", "default": 7},
                "days_back":  {"type": "integer", "description": "How many days back to look (default 0)", "default": 0}
            }
        }
    },
    {
        "name": "create_calendar_event",
        "description": "Create a new calendar event for George.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":          {"type": "string",  "description": "Event title"},
                "date":           {"type": "string",  "description": "Date in YYYY-MM-DD format"},
                "start_time":     {"type": "string",  "description": "Start time in HH:MM 24hr format"},
                "duration_hours": {"type": "number",  "description": "Duration in hours (default 1)", "default": 1},
                "description":    {"type": "string",  "description": "Optional description", "default": ""}
            },
            "required": ["title", "date", "start_time"]
        }
    },
    {
        "name": "send_email",
        "description": "Send an email on George's behalf.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to":      {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject"},
                "body":    {"type": "string", "description": "Email body text"}
            },
            "required": ["to", "subject", "body"]
        }
    },
    {
        "name": "get_accounting_data",
        "description": "Get George's accounting data including invoice summary and recent invoices. Use when he asks about expenses, spending, invoices, or financial data.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "web_search",
        "description": "Search the web for current information, news, prices, or anything that requires up-to-date data.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "clear_accounting_sheet",
        "description": "Clear all invoice data from the accounting spreadsheet. Only use when George explicitly asks to clear or reset the sheet.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    }
]

# ── Tool Executor ─────────────────────────────────────────────────────────────
def execute_tool(tool_name, tool_input, chat_id):
    """Execute a tool and return the result."""
    print(f"Executing tool: {tool_name} with input: {json.dumps(tool_input)[:200]}")
    if tool_name == "search_emails":
        return tool_search_emails(
            tool_input.get("query","in:inbox newer_than:7d"),
            tool_input.get("max_results", 8)
        )
    elif tool_name == "read_email":
        return tool_read_email(tool_input.get("email_id",""))
    elif tool_name == "send_attachment_to_telegram":
        return tool_send_attachment_to_telegram(
            chat_id,
            tool_input.get("email_id",""),
            tool_input.get("attachment_id",""),
            tool_input.get("filename","file")
        )
    elif tool_name == "get_calendar_events":
        return tool_get_calendar_events(
            tool_input.get("days_ahead", 7),
            tool_input.get("days_back", 0)
        )
    elif tool_name == "create_calendar_event":
        return tool_create_calendar_event(
            tool_input.get("title",""),
            tool_input.get("date",""),
            tool_input.get("start_time","09:00"),
            tool_input.get("duration_hours", 1),
            tool_input.get("description","")
        )
    elif tool_name == "send_email":
        return tool_send_email(
            tool_input.get("to",""),
            tool_input.get("subject",""),
            tool_input.get("body","")
        )
    elif tool_name == "get_accounting_data":
        return tool_get_accounting_data()
    elif tool_name == "web_search":
        return tool_web_search(tool_input.get("query",""))
    elif tool_name == "clear_accounting_sheet":
        return tool_clear_accounting_sheet()
    else:
        return {"error": f"Unknown tool: {tool_name}"}

# ── Agentic Loop ──────────────────────────────────────────────────────────────
def run_agent(chat_id, user_message):
    """
    Run the Claude agent loop.
    Claude can call multiple tools in sequence until it has all the info it needs.
    """
    history  = get_history(chat_id)
    messages = history + [{"role": "user", "content": user_message}]

    max_iterations = 8
    iterations     = 0

    while iterations < max_iterations:
        iterations += 1

        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages
        )

        # Check stop reason
        if response.stop_reason == "end_turn":
            # Claude is done — extract final text response
            final_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    final_text += block.text
            return final_text

        elif response.stop_reason == "tool_use":
            # Claude wants to use tools
            # Add Claude's response (with tool calls) to messages
            messages.append({"role": "assistant", "content": response.content})

            # Execute all requested tools
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = execute_tool(block.name, block.input, chat_id)
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     json.dumps(result)
                    })

            # Add tool results to messages
            messages.append({"role": "user", "content": tool_results})

        else:
            # Unexpected stop reason
            break

    return "I ran into an issue processing that request. Please try again."

# ── Daily Business Lesson ─────────────────────────────────────────────────────
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

# ── Morning Briefing ──────────────────────────────────────────────────────────
def build_morning_briefing():
    today    = datetime.datetime.utcnow() - datetime.timedelta(hours=4)
    day_str  = today.strftime("%A, %B %d, %Y")
    lines    = ["Good morning, " + OWNER_NAME + "!", day_str, ""]

    try:
        events = tool_get_calendar_events(days_ahead=1)
        if events.get("events"):
            lines.append("Today's Schedule:")
            for e in events["events"]:
                lines.append("  " + e["title"] + " — " + e["time"])
        else:
            lines.append("Calendar: Nothing scheduled today.")
    except:
        lines.append("Calendar: unavailable")

    lines.append("")

    try:
        data = tool_get_accounting_data()
        s    = data.get("summary",{})
        if s.get("status") == "ok":
            lines.append("Expenses:")
            lines.append("  This week: TTD " + str(round(s.get("total_spend_this_week",0),2)))
            lines.append("  This month: TTD " + str(round(s.get("total_spend_this_month",0),2)))
    except:
        lines.append("Expenses: unavailable")

    lines.append("")

    try:
        lines.append(get_daily_lesson())
    except:
        pass

    lines.append("")
    lines.append("Type anything to get started. I'm here!")
    return "\n".join(lines)

# ── Schedulers ────────────────────────────────────────────────────────────────
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
            except Exception as e:
                print("Briefing error: " + str(e))
        time.sleep(60)

def event_reminder_scheduler():
    while True:
        try:
            if OWNER_CHAT_ID:
                events = tool_get_calendar_events(days_ahead=0)
                now    = datetime.datetime.utcnow()
                for e in events.get("events", []):
                    time_str = e.get("time","")
                    try:
                        dt   = datetime.datetime.strptime(time_str, "%A, %B %d at %I:%M %p")
                        dt   = dt.replace(year=now.year)
                        diff = (dt - (now - datetime.timedelta(hours=4))).total_seconds() / 60
                        if 25 <= diff <= 35:
                            send_message(OWNER_CHAT_ID,
                                "Reminder: " + e["title"] + " starts in 30 minutes!")
                    except:
                        pass
        except:
            pass
        time.sleep(300)

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_message(chat_id, text, parse_mode=""):
    if len(text) > 4000:
        for i in range(0, len(text), 4000):
            requests.post(TELEGRAM_API + "/sendMessage",
                json={"chat_id": chat_id, "text": text[i:i+4000]})
            time.sleep(0.3)
    else:
        requests.post(TELEGRAM_API + "/sendMessage",
            json={"chat_id": chat_id, "text": text})

def get_file_url(file_id):
    r    = requests.get(TELEGRAM_API + "/getFile", params={"file_id": file_id})
    path = r.json()["result"]["file_path"]
    return "https://api.telegram.org/file/bot" + TELEGRAM_TOKEN + "/" + path

@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    update  = request.json
    if not update:
        return "ok"
    msg     = update.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    text    = msg.get("text","").strip()
    text_l  = text.lower()
    photo   = msg.get("photo")
    document= msg.get("document")

    if not chat_id:
        return "ok"

    # Handle /start and /help
    if text_l in ("/start","/help","help"):
        send_message(chat_id,
            "Hey " + OWNER_NAME + "! I'm Primo, your personal business assistant.\n\n"
            "Just talk to me naturally. I can:\n\n"
            "- Read and search your emails\n"
            "- Find and send you file attachments\n"
            "- Check and update your calendar\n"
            "- Track your expenses and invoices\n"
            "- Send emails on your behalf\n"
            "- Search the web\n"
            "- Give you a daily briefing\n\n"
            "No special commands needed — just tell me what you need!"
        )
        return "ok"

    if text_l in ("/clearmemory","forget","clear memory"):
        clear_history(chat_id)
        send_message(chat_id, "Memory cleared! Fresh start.")
        return "ok"

    if text_l in ("/briefing","briefing","good morning","morning update"):
        def send_briefing():
            send_message(chat_id, build_morning_briefing())
        threading.Thread(target=send_briefing, daemon=True).start()
        return "ok"

    # Handle photo/document upload
    if photo or (document and document.get("mime_type","").startswith("image")):
        def process_image():
            try:
                send_message(chat_id, "Got it — reading this invoice...")
                file_id   = photo[-1]["file_id"] if photo else document["file_id"]
                file_url  = get_file_url(file_id)
                img_data  = requests.get(file_url).content
                image_b64 = base64.standard_b64encode(img_data).decode("utf-8")
                resp = claude.messages.create(
                    model="claude-sonnet-4-5", max_tokens=400,
                    messages=[{"role":"user","content":[
                        {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":image_b64}},
                        {"type":"text","text":
                            "Extract invoice data and return ONLY valid JSON: "
                            "{vendor,invoice_date,invoice_number,description,category,currency,amount,tax,confidence}. "
                            "Categories: Office Supplies|Software/Cloud|Shipping|Facilities|Marketing|Travel|Utilities|Professional Services|Food & Entertainment|Other. "
                            "Default currency TTD."}
                    ]}]
                )
                raw = resp.content[0].text.strip()
                if raw.startswith("```"):
                    raw = raw.split("```")[1]
                    if raw.startswith("json"): raw = raw[4:]
                data = json.loads(raw)
                if ACCOUNTING_API_URL:
                    r      = requests.post(ACCOUNTING_API_URL + "/api/log_invoice",
                                           json={"data": data, "source": "telegram_photo"}, timeout=15)
                    inv_id = r.json().get("inv_id","?")
                else:
                    inv_id = "N/A"
                amount = float(data.get("amount",0))
                tax    = float(data.get("tax",0))
                send_message(chat_id,
                    "Logged!\n\n"
                    "ID: " + str(inv_id) + "\n"
                    "Vendor: " + data.get("vendor","?") + "\n"
                    "Date: " + data.get("invoice_date","?") + "\n"
                    "Category: " + data.get("category","Other") + "\n"
                    "Amount: " + data.get("currency","TTD") + " " + str(amount) + "\n"
                    "Tax: " + data.get("currency","TTD") + " " + str(tax) + "\n"
                    "Total: " + data.get("currency","TTD") + " " + str(round(amount+tax,2))
                )
            except Exception as e:
                send_message(chat_id, "Error reading invoice: " + str(e)[:100])
        threading.Thread(target=process_image, daemon=True).start()
        return "ok"

    # All other messages — run through the agent
    if text:
        def run_in_background():
            try:
                reply = run_agent(chat_id, text)
                add_to_history(chat_id, "user", text)
                add_to_history(chat_id, "assistant", reply)
                send_message(chat_id, reply)
            except Exception as e:
                traceback.print_exc()
                send_message(chat_id, "Something went wrong: " + str(e)[:100])
        threading.Thread(target=run_in_background, daemon=True).start()

    return "ok"

@app.route("/", methods=["GET"])
def health():
    return {"status":"ok","bot":"Primo v2","time":str(datetime.datetime.now())}

threading.Thread(target=morning_scheduler,        daemon=True).start()
threading.Thread(target=event_reminder_scheduler, daemon=True).start()

if __name__ == "__main__":
    print("Primo v2 starting on http://localhost:5002")
    app.run(host="0.0.0.0", port=5002, debug=False)

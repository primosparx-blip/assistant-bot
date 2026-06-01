"""
PrimoAssistanttBot — Telegram Personal Assistant
Commander agent: orchestrates all other agents, manages Gmail + Google Calendar
"""
import os, json, base64, datetime, requests, traceback, threading, time
from flask import Flask, request, jsonify
import anthropic
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import email as emaillib
from email.mime.text import MIMEText

app = Flask(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN        = os.environ["ASSISTANT_BOT_TOKEN"]
ANTHROPIC_API_KEY     = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_TOKEN_JSON     = os.environ.get("GOOGLE_TOKEN_JSON", "")
ACCOUNTING_API_URL    = os.environ.get("ACCOUNTING_API_URL", "")  # Railway URL of accounting bot
OWNER_CHAT_ID         = os.environ.get("OWNER_CHAT_ID", "")       # Your Telegram chat ID
TELEGRAM_API          = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar"
]

# ── Google Auth ─────────────────────────────────────────────────────────────
def get_google_creds():
    if GOOGLE_TOKEN_JSON:
        token_data = json.loads(GOOGLE_TOKEN_JSON)
    else:
        with open("token.json") as f:
            token_data = json.load(f)
    creds = Credentials.from_authorized_user_info(token_data, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds

# ── Gmail ───────────────────────────────────────────────────────────────────
def get_gmail_service():
    return build("gmail", "v1", credentials=get_google_creds())

def get_unread_emails(max_results=5):
    service = get_gmail_service()
    results = service.users().messages().list(
        userId="me", labelIds=["INBOX","UNREAD"], maxResults=max_results
    ).execute()
    messages = results.get("messages", [])
    emails = []
    for msg in messages:
        detail = service.users().messages().get(
            userId="me", id=msg["id"], format="metadata",
            metadataHeaders=["From","Subject","Date"]
        ).execute()
        headers = {h["name"]:h["value"] for h in detail["payload"]["headers"]}
        snippet = detail.get("snippet","")
        emails.append({
            "id": msg["id"],
            "from": headers.get("From",""),
            "subject": headers.get("Subject",""),
            "date": headers.get("Date",""),
            "snippet": snippet[:120]
        })
    return emails

def send_email(to, subject, body):
    service = get_gmail_service()
    message = MIMEText(body)
    message["to"]      = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw":raw}).execute()

def draft_email_with_claude(instruction):
    """Use Claude to draft an email from a natural language instruction."""
    response = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=512,
        messages=[{"role":"user","content":
            f"Draft a professional email based on this instruction: {instruction}\n\n"
            "Return ONLY valid JSON: "
            '{"to":"email@example.com","subject":"Subject here","body":"Email body here"}\n'
            "If no recipient is mentioned use to: unknown@unknown.com"
        }]
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
    return json.loads(raw)

# ── Google Calendar ─────────────────────────────────────────────────────────
def get_calendar_service():
    return build("calendar", "v3", credentials=get_google_creds())

def get_todays_events():
    service   = get_calendar_service()
    now       = datetime.datetime.utcnow()
    start     = now.replace(hour=0, minute=0, second=0).isoformat() + "Z"
    end       = now.replace(hour=23, minute=59, second=59).isoformat() + "Z"
    events_result = service.events().list(
        calendarId="primary", timeMin=start, timeMax=end,
        singleEvents=True, orderBy="startTime"
    ).execute()
    return events_result.get("items", [])

def get_weeks_events():
    service   = get_calendar_service()
    now       = datetime.datetime.utcnow()
    start     = now.isoformat() + "Z"
    end       = (now + datetime.timedelta(days=7)).isoformat() + "Z"
    events_result = service.events().list(
        calendarId="primary", timeMin=start, timeMax=end,
        singleEvents=True, orderBy="startTime", maxResults=20
    ).execute()
    return events_result.get("items", [])

def create_calendar_event(summary, start_dt, end_dt, description=""):
    service = get_calendar_service()
    event   = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/Port_of_Spain"},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "America/Port_of_Spain"},
    }
    return service.events().insert(calendarId="primary", body=event).execute()

def parse_event_with_claude(instruction):
    """Use Claude to parse a natural language scheduling request."""
    today = datetime.date.today().isoformat()
    response = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=256,
        messages=[{"role":"user","content":
            f"Today is {today}. Parse this scheduling request: '{instruction}'\n\n"
            "Return ONLY valid JSON:\n"
            '{"summary":"Event title","date":"YYYY-MM-DD","start_time":"HH:MM","duration_hours":1,"description":""}\n'
            "Use 24-hour time. If no duration mentioned assume 1 hour."
        }]
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
    return json.loads(raw)

def format_event(event):
    start = event.get("start",{})
    time_str = start.get("dateTime","") or start.get("date","")
    try:
        dt = datetime.datetime.fromisoformat(time_str.replace("Z",""))
        time_str = dt.strftime("%a %b %d, %I:%M %p")
    except:
        pass
    return f"📅 *{event.get('summary','No title')}*\n   {time_str}"

# ── Orchestrator: query all agents ──────────────────────────────────────────
def get_full_business_briefing():
    lines = [f"🌅 *Good morning George!*\n_{datetime.date.today().strftime('%A, %B %d, %Y')}_\n"]

    # Calendar
    try:
        events = get_todays_events()
        if events:
            lines.append(f"📅 *Today's Schedule ({len(events)} events):*")
            for e in events[:5]:
                lines.append(f"  {format_event(e)}")
        else:
            lines.append("📅 *Calendar:* Nothing scheduled today")
    except Exception as e:
        lines.append(f"📅 Calendar: unavailable")

    # Email
    try:
        emails = get_unread_emails(3)
        if emails:
            lines.append(f"\n📧 *Unread Emails ({len(emails)}):*")
            for em in emails:
                lines.append(f"  • From: {em['from'][:30]}\n    {em['subject'][:50]}")
        else:
            lines.append("\n📧 *Email:* Inbox is clear ✅")
    except Exception as e:
        lines.append("\n📧 Email: unavailable")

    # Accounting Agent
    if ACCOUNTING_API_URL:
        try:
            r = requests.get(f"{ACCOUNTING_API_URL}/api/summary", timeout=10)
            data = r.json()
            if data.get("status") == "ok":
                lines.append(
                    f"\n📊 *Accounting:*\n"
                    f"  • Pending invoices: {data.get('pending_invoices',0)}\n"
                    f"  • This week: ${data.get('total_spend_this_week',0):,.2f}\n"
                    f"  • This month: ${data.get('total_spend_this_month',0):,.2f}"
                )
        except:
            lines.append("\n📊 Accounting: unavailable")

    lines.append("\n_Type /help for all commands_")
    return "\n".join(lines)

# ── Telegram helpers ────────────────────────────────────────────────────────
def send_message(chat_id, text, parse_mode="Markdown"):
    requests.post(f"{TELEGRAM_API}/sendMessage",
                  json={"chat_id":chat_id,"text":text,"parse_mode":parse_mode})

# ── Morning briefing scheduler ──────────────────────────────────────────────
def morning_scheduler():
    """Send daily briefing at 8am Trinidad time (12:00 UTC)."""
    while True:
        now = datetime.datetime.utcnow()
        if now.hour == 12 and now.minute < 5 and OWNER_CHAT_ID:
            try:
                briefing = get_full_business_briefing()
                send_message(OWNER_CHAT_ID, briefing)
            except Exception as e:
                print(f"Morning briefing error: {e}")
            time.sleep(360)
        time.sleep(60)

# ── Main webhook ─────────────────────────────────────────────────────────────
@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    update  = request.json
    if not update:
        return "ok"

    msg     = update.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    text    = msg.get("text", "").strip()
    text_l  = text.lower()

    if not chat_id:
        return "ok"

    try:
        # ── /start or /help ───────────────────────────────────────────────
        if text_l in ("/start", "/help", "help"):
            send_message(chat_id,
                "👋 *Primo Personal Assistant*\n\n"
                "I manage your calendar, email, and coordinate all your agents.\n\n"
                "*📅 Calendar:*\n"
                "• /today — today's schedule\n"
                "• /week — this week's events\n"
                "• /schedule [event] — create an event\n\n"
                "*📧 Email:*\n"
                "• /emails — unread emails\n"
                "• /send [instruction] — draft & send email\n\n"
                "*🏢 Business:*\n"
                "• /briefing — full business update\n"
                "• /accounting — accounting summary\n\n"
                "*💬 Natural language:*\n"
                "Just type naturally — I understand plain English!"
            )

        # ── Briefing ──────────────────────────────────────────────────────
        elif text_l in ("/briefing", "briefing", "update", "morning"):
            send_message(chat_id, "⏳ Getting your briefing...")
            send_message(chat_id, get_full_business_briefing())

        # ── Calendar: today ───────────────────────────────────────────────
        elif text_l in ("/today", "today", "what's today", "whats today"):
            events = get_todays_events()
            if not events:
                send_message(chat_id, "📅 Nothing on your calendar today! Free day ✅")
            else:
                lines = [f"📅 *Today — {datetime.date.today().strftime('%A, %B %d')}*\n"]
                for e in events:
                    lines.append(format_event(e))
                send_message(chat_id, "\n".join(lines))

        # ── Calendar: week ────────────────────────────────────────────────
        elif text_l in ("/week", "week", "this week", "weekly schedule"):
            events = get_weeks_events()
            if not events:
                send_message(chat_id, "📅 Nothing in your calendar this week!")
            else:
                lines = ["📅 *This Week:*\n"]
                for e in events[:10]:
                    lines.append(format_event(e))
                send_message(chat_id, "\n".join(lines))

        # ── Calendar: schedule ────────────────────────────────────────────
        elif text_l.startswith("/schedule") or any(w in text_l for w in ["schedule","book","set up a meeting","add to calendar","remind me"]):
            instruction = text.replace("/schedule","").strip() or text
            send_message(chat_id, f"📅 Scheduling: _{instruction}_...")
            parsed = parse_event_with_claude(instruction)
            date_parts = parsed["date"].split("-")
            start_dt = datetime.datetime(
                int(date_parts[0]), int(date_parts[1]), int(date_parts[2]),
                int(parsed["start_time"].split(":")[0]),
                int(parsed["start_time"].split(":")[1])
            )
            end_dt = start_dt + datetime.timedelta(hours=float(parsed.get("duration_hours",1)))
            event  = create_calendar_event(
                parsed["summary"], start_dt, end_dt, parsed.get("description","")
            )
            send_message(chat_id,
                f"✅ *Event Created!*\n\n"
                f"📅 {parsed['summary']}\n"
                f"🕐 {start_dt.strftime('%A, %B %d at %I:%M %p')}\n"
                f"⏱ Duration: {parsed.get('duration_hours',1)} hour(s)"
            )

        # ── Email: read ───────────────────────────────────────────────────
        elif text_l in ("/emails", "emails", "check email", "unread", "inbox"):
            send_message(chat_id, "📧 Checking your inbox...")
            emails = get_unread_emails(5)
            if not emails:
                send_message(chat_id, "📧 No unread emails! Inbox is clear ✅")
            else:
                lines = [f"📧 *{len(emails)} Unread Emails:*\n"]
                for i,em in enumerate(emails,1):
                    lines.append(
                        f"*{i}.* {em['subject'][:50]}\n"
                        f"   From: {em['from'][:40]}\n"
                        f"   _{em['snippet'][:80]}_\n"
                    )
                send_message(chat_id, "\n".join(lines))

        # ── Email: send ───────────────────────────────────────────────────
        elif text_l.startswith("/send") or any(w in text_l for w in ["send email","email to","write to","draft email"]):
            instruction = text.replace("/send","").strip() or text
            send_message(chat_id, f"📧 Drafting email: _{instruction}_...")
            draft = draft_email_with_claude(instruction)
            send_message(chat_id,
                f"📧 *Email Ready to Send:*\n\n"
                f"To: {draft['to']}\n"
                f"Subject: {draft['subject']}\n\n"
                f"_{draft['body'][:300]}_\n\n"
                f"Reply *confirm* to send or *cancel* to discard."
            )
            # Store pending draft in simple memory
            app.pending_drafts = getattr(app, "pending_drafts", {})
            app.pending_drafts[chat_id] = draft

        # ── Confirm email send ────────────────────────────────────────────
        elif text_l in ("confirm","yes","send it") and hasattr(app,"pending_drafts") and chat_id in app.pending_drafts:
            draft = app.pending_drafts.pop(chat_id)
            send_email(draft["to"], draft["subject"], draft["body"])
            send_message(chat_id, f"✅ Email sent to *{draft['to']}*!")

        elif text_l in ("cancel","no","discard") and hasattr(app,"pending_drafts") and chat_id in app.pending_drafts:
            app.pending_drafts.pop(chat_id)
            send_message(chat_id, "❌ Email discarded.")

        # ── Accounting summary ────────────────────────────────────────────
        elif text_l in ("/accounting","accounting","expenses","spending"):
            if ACCOUNTING_API_URL:
                r    = requests.get(f"{ACCOUNTING_API_URL}/api/summary", timeout=10)
                data = r.json()
                if data.get("status") == "ok":
                    top = "\n".join([f"  • {c['category']}: ${c['amount']:,.2f}"
                                     for c in data.get("top_categories",[])])
                    send_message(chat_id,
                        f"📊 *Accounting Summary*\n\n"
                        f"📋 Total invoices: {data.get('total_invoices',0)}\n"
                        f"⏳ Pending: {data.get('pending_invoices',0)}\n"
                        f"💵 This week: ${data.get('total_spend_this_week',0):,.2f}\n"
                        f"📅 This month: ${data.get('total_spend_this_month',0):,.2f}\n"
                        f"💰 All time: ${data.get('total_spend_all_time',0):,.2f}\n\n"
                        f"📂 *Top Categories:*\n{top}"
                    )
            else:
                send_message(chat_id, "⚠️ Accounting Agent URL not configured. Add ACCOUNTING_API_URL to Railway variables.")

        # ── Natural language fallback via Claude ──────────────────────────
        else:
            response = claude.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=300,
                messages=[{"role":"user","content":
                    f"You are a personal assistant. The user said: '{text}'\n"
                    "Respond helpfully in 2-3 sentences. If they're asking to do something "
                    "with calendar/email/accounting, tell them the exact command to use. "
                    "Available commands: /today /week /schedule /emails /send /briefing /accounting"
                }]
            )
            send_message(chat_id, response.content[0].text)

    except Exception as e:
        traceback.print_exc()
        send_message(chat_id, f"❌ Error: {str(e)[:100]}\n\nTry /help for available commands.")

    return "ok"

@app.route("/", methods=["GET"])
def health():
    return {"status":"ok","bot":"PrimoAssistanttBot","time":str(datetime.datetime.now())}

# Start morning briefing scheduler
threading.Thread(target=morning_scheduler, daemon=True).start()

if __name__ == "__main__":
    print("PrimoAssistanttBot starting on http://localhost:5002")
    app.run(host="0.0.0.0", port=5002, debug=False)

"""
PrimoAssistanttBot - Telegram Personal Assistant
Commander agent with daily briefings, Gmail, Calendar, and business lessons
"""
import os, json, base64, datetime, hashlib, requests, traceback, threading, time
from flask import Flask, request, jsonify
import anthropic
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from email.mime.text import MIMEText

app = Flask(__name__)

TELEGRAM_TOKEN     = os.environ["ASSISTANT_BOT_TOKEN"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_TOKEN_JSON  = os.environ.get("GOOGLE_TOKEN_JSON", "")
ACCOUNTING_API_URL = os.environ.get("ACCOUNTING_API_URL", "")
OWNER_CHAT_ID      = os.environ.get("OWNER_CHAT_ID", "")
TELEGRAM_API       = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar"
]

BUSINESS_CONCEPTS = [
    "opportunity cost", "cash flow", "gross margin", "EBITDA",
    "working capital", "accounts receivable", "break even point", "return on investment",
    "cost of goods sold", "net profit margin", "liquidity", "economies of scale",
    "fixed vs variable costs", "price elasticity", "brand equity",
    "customer lifetime value", "churn rate", "gross profit",
    "overheads", "markup vs margin", "debtors and creditors",
    "accounts payable", "depreciation", "inventory turnover", "profit margin"
]

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

def get_gmail_service():
    return build("gmail", "v1", credentials=get_google_creds())

def get_calendar_service():
    return build("calendar", "v3", credentials=get_google_creds())

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
        emails.append({
            "id": msg["id"],
            "from": headers.get("From",""),
            "subject": headers.get("Subject",""),
            "date": headers.get("Date",""),
            "snippet": detail.get("snippet","")[:120]
        })
    return emails

def get_email_body(service, msg_id):
    """Fetch full email body text."""
    try:
        detail = service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()
        payload = detail.get("payload", {})
        
        def extract_text(part):
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    import base64
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            for sub in part.get("parts", []):
                result = extract_text(sub)
                if result:
                    return result
            return ""
        
        return extract_text(payload)[:3000]
    except:
        return ""

def scan_emails_for_receipts(days=10):
    """Scan Gmail for receipts and invoices from the past N days."""
    service = get_gmail_service()
    
    # Search for receipt/invoice emails
    query = "subject:(receipt OR invoice OR payment OR order confirmation OR booking) newer_than:" + str(days) + "d"
    
    results = service.users().messages().list(
        userId="me", q=query, maxResults=20
    ).execute()
    
    messages = results.get("messages", [])
    receipts = []
    
    for msg in messages:
        try:
            detail  = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From","Subject","Date"]
            ).execute()
            headers = {h["name"]:h["value"] for h in detail["payload"]["headers"]}
            body    = get_email_body(service, msg["id"])
            
            receipts.append({
                "id":      msg["id"],
                "from":    headers.get("From",""),
                "subject": headers.get("Subject",""),
                "date":    headers.get("Date",""),
                "body":    body
            })
        except:
            pass
    
    return receipts

def extract_invoice_from_email(email):
    """Use Claude to extract invoice data from email content."""
    email_text = (
        "From: " + email["from"] + " Subject: " + email["subject"] +
        " Date: " + email["date"] + " Body: " + email["body"][:2000]
    )
    instruction = (
        "You are an accounting AI. Extract invoice/receipt data from this email. "
        + email_text +
        " Return ONLY valid JSON or the word SKIP if not a receipt: "
        '{"vendor":"Company name","invoice_date":"YYYY-MM-DD","invoice_number":"ref or N/A",'
        '"description":"brief description","category":"Food & Entertainment or Travel or '
        'Software/Cloud or Utilities or Professional Services or Other",'
        '"currency":"TTD","amount":0.00,"tax":0.00,"confidence":"high or medium or low"}'
    )
    resp = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=300,
        messages=[{"role": "user", "content": instruction}]
    )
    raw = resp.content[0].text.strip()
    if raw.upper().startswith("SKIP") or raw == "":
        return None
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw)
    except:
        return None

def scan_and_log_receipts(days=10):
    """Scan emails, extract receipts, log to accounting bot, return summary."""
    emails   = scan_emails_for_receipts(days)
    logged   = []
    skipped  = 0

    for email in emails:
        data = extract_invoice_from_email(email)
        if not data:
            skipped += 1
            continue
        # Log to accounting bot
        try:
            if ACCOUNTING_API_URL:
                resp = requests.post(
                    ACCOUNTING_API_URL + "/api/log_invoice",
                    json={"data": data, "source": "email_scan"},
                    timeout=15
                )
                result = resp.json()
                if result.get("status") == "ok":
                    logged.append({
                        "vendor":  data.get("vendor","Unknown"),
                        "amount":  data.get("amount",0),
                        "total":   round(float(data.get("amount",0)) + float(data.get("tax",0)), 2),
                        "inv_id":  result.get("inv_id",""),
                        "subject": email["subject"][:50]
                    })
        except Exception as e:
            print("Log error: " + str(e))

    return {"logged": logged, "skipped": skipped, "total_scanned": len(emails)}


def send_email(to, subject, body):
    service  = get_gmail_service()
    message  = MIMEText(body)
    message["to"]      = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw":raw}).execute()

def draft_email_with_claude(instruction):
    response = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=512,
        messages=[{"role":"user","content":
            "Draft a professional email based on this instruction: " + instruction +
            "\n\nReturn ONLY valid JSON: "
            '{"to":"email@example.com","subject":"Subject here","body":"Email body here"}'
        }]
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
    return json.loads(raw)

def get_todays_events():
    service = get_calendar_service()
    now     = datetime.datetime.utcnow()
    start   = now.replace(hour=0,  minute=0,  second=0).isoformat()  + "Z"
    end     = now.replace(hour=23, minute=59, second=59).isoformat() + "Z"
    result  = service.events().list(
        calendarId="primary", timeMin=start, timeMax=end,
        singleEvents=True, orderBy="startTime"
    ).execute()
    return result.get("items", [])

def get_weeks_events():
    service = get_calendar_service()
    now     = datetime.datetime.utcnow()
    start   = now.isoformat() + "Z"
    end     = (now + datetime.timedelta(days=7)).isoformat() + "Z"
    result  = service.events().list(
        calendarId="primary", timeMin=start, timeMax=end,
        singleEvents=True, orderBy="startTime", maxResults=20
    ).execute()
    return result.get("items", [])

def create_calendar_event(summary, start_dt, end_dt, description=""):
    service = get_calendar_service()
    event   = {
        "summary":     summary,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/Port_of_Spain"},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "America/Port_of_Spain"},
    }
    return service.events().insert(calendarId="primary", body=event).execute()

def parse_event_with_claude(instruction):
    today = datetime.date.today().isoformat()
    response = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=256,
        messages=[{"role":"user","content":
            "Today is " + today + ". Parse this scheduling request: '" + instruction + "'\n\n"
            'Return ONLY valid JSON: {"summary":"Event title","date":"YYYY-MM-DD",'
            '"start_time":"HH:MM","duration_hours":1,"description":""}\n'
            "Use 24hr time. Default duration 1 hour if not mentioned."
        }]
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
    return json.loads(raw)

def format_event(event):
    start    = event.get("start", {})
    time_str = start.get("dateTime","") or start.get("date","")
    try:
        dt       = datetime.datetime.fromisoformat(time_str.replace("Z",""))
        dt_local = dt - datetime.timedelta(hours=4)
        time_str = dt_local.strftime("%I:%M %p")
    except:
        pass
    return "  - " + event.get("summary","No title") + " at " + time_str

def get_daily_lesson():
    today_str = datetime.date.today().isoformat()
    idx       = int(hashlib.md5(today_str.encode()).hexdigest(), 16) % len(BUSINESS_CONCEPTS)
    concept   = BUSINESS_CONCEPTS[idx]
    prompt    = (
        "Explain the business concept of " + concept + " in 2-3 simple sentences. "
        "Use a practical example a restaurant owner in Trinidad would relate to. "
        "Keep it under 60 words. Do not use any markdown formatting."
    )
    resp = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=120,
        messages=[{"role":"user","content": prompt}]
    )
    return "Today's Business Lesson: " + concept.title() + "\n\n" + resp.content[0].text

def get_accounting_context():
    if not ACCOUNTING_API_URL:
        return ""
    try:
        sum_r  = requests.get(ACCOUNTING_API_URL + "/api/summary", timeout=10)
        inv_r  = requests.get(ACCOUNTING_API_URL + "/api/invoices?limit=50", timeout=10)
        sdata  = sum_r.json()
        idata  = inv_r.json()
        if sdata.get("status") != "ok":
            return ""
        invoices  = idata.get("invoices", [])
        inv_lines = []
        for inv in invoices:
            inv_lines.append(
                "ID:" + str(inv.get("ID","")) + " | " +
                str(inv.get("Date Received","")) + " | " +
                str(inv.get("Vendor","")) + " | " +
                str(inv.get("Category","")) + " | TTD " +
                str(inv.get("Amount",0)) + " | Tax: TTD " +
                str(inv.get("Tax",0)) + " | Total: TTD " +
                str(inv.get("Total",0)) + " | Status: " +
                str(inv.get("Status",""))
            )
        top = ", ".join([
            c["category"] + ": TTD " + str(round(c["amount"],2))
            for c in sdata.get("top_categories",[])
        ])
        return (
            "\n\nAccounting data - " +
            "Total invoices: " + str(sdata.get("total_invoices",0)) + ", " +
            "Pending: " + str(sdata.get("pending_invoices",0)) + ", " +
            "This week: TTD " + str(sdata.get("total_spend_this_week",0)) + ", " +
            "This month: TTD " + str(sdata.get("total_spend_this_month",0)) + ", " +
            "All time: TTD " + str(sdata.get("total_spend_all_time",0)) + ", " +
            "Top categories: " + top +
            "\n\nAll invoices:\n" + "\n".join(inv_lines)
        )
    except Exception as e:
        return ""

def web_search(query):
    """Search the web using DuckDuckGo instant answer API - no key needed."""
    try:
        r = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            timeout=10
        )
        data = r.json()
        results = []

        # Abstract (main answer)
        if data.get("AbstractText"):
            results.append(data["AbstractText"])

        # Related topics
        for topic in data.get("RelatedTopics", [])[:3]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append(topic["Text"])

        if results:
            return " | ".join(results[:3])
        else:
            return "No results found for: " + query

    except Exception as e:
        return "Search error: " + str(e)

def web_search(query):
    try:
        r = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            timeout=10
        )
        data = r.json()
        results = []
        if data.get("AbstractText"):
            results.append(data["AbstractText"])
        for topic in data.get("RelatedTopics", [])[:3]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append(topic["Text"])
        return (" ".join(results[:3])) if results else "No results found for: " + query
    except Exception as e:
        return "Search error: " + str(e)


def get_full_briefing():
    today    = datetime.datetime.utcnow() - datetime.timedelta(hours=4)
    hour     = today.hour
    greeting = "Good morning" if hour < 12 else ("Good afternoon" if hour < 17 else "Good evening")
    day_str  = today.strftime("%A, %B %d, %Y")
    lines    = [greeting + " George!", day_str, ""]

    # Calendar
    try:
        events = get_todays_events()
        if events:
            lines.append("Today's Schedule (" + str(len(events)) + " events):")
            for e in events[:8]:
                lines.append(format_event(e))
        else:
            lines.append("Calendar: No events today - free day!")
    except:
        lines.append("Calendar: unavailable")

    lines.append("")

    # Email
    try:
        emails = get_unread_emails(5)
        if emails:
            lines.append("Unread Emails (" + str(len(emails)) + "):")
            for em in emails[:5]:
                sender  = em["from"].split("<")[0].strip()[:30]
                subject = em["subject"][:50]
                lines.append("  - " + sender + ": " + subject)
        else:
            lines.append("Email: Inbox clear!")
    except:
        lines.append("Email: unavailable")

    lines.append("")

    # Expenses
    if ACCOUNTING_API_URL:
        try:
            sum_r  = requests.get(ACCOUNTING_API_URL + "/api/summary", timeout=10)
            inv_r  = requests.get(ACCOUNTING_API_URL + "/api/invoices?limit=5", timeout=10)
            sdata  = sum_r.json()
            idata  = inv_r.json()
            if sdata.get("status") == "ok":
                lines.append("Expenses:")
                lines.append("  - This week: TTD " + str(round(sdata.get("total_spend_this_week",0),2)))
                lines.append("  - This month: TTD " + str(round(sdata.get("total_spend_this_month",0),2)))
                lines.append("  - Pending invoices: " + str(sdata.get("pending_invoices",0)))
                top = sdata.get("top_categories",[])
                if top:
                    lines.append("  - Top categories:")
                    for c in top[:3]:
                        lines.append("    * " + c["category"] + ": TTD " + str(round(c["amount"],2)))
                invoices = idata.get("invoices",[])
                if invoices:
                    lines.append("  - Recent invoices:")
                    for inv in list(reversed(invoices))[:3]:
                        lines.append("    * " + str(inv.get("Vendor","")) + " TTD " + str(inv.get("Total",0)))
        except:
            lines.append("Expenses: unavailable")

    lines.append("")
    lines.append("---")

    # Daily business lesson
    try:
        lesson = get_daily_lesson()
        lines.append(lesson)
    except Exception as e:
        print("Lesson error: " + str(e))

    lines.append("")
    lines.append("Type /help for all commands")
    return "\n".join(lines)

def send_message(chat_id, text, parse_mode="Markdown"):
    requests.post(
        TELEGRAM_API + "/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    )

def morning_scheduler():
    last_sent = None
    while True:
        now  = datetime.datetime.utcnow()
        date = now.date()
        if now.hour == 12 and now.minute < 5 and OWNER_CHAT_ID and last_sent != date:
            try:
                print("Sending morning briefing at " + str(now))
                # Scan emails for overnight receipts
                try:
                    scan_result = scan_and_log_receipts(1)  # last 24 hours only
                    if scan_result["logged"]:
                        vendors = ", ".join([i["vendor"] for i in scan_result["logged"][:5]])
                        send_message(OWNER_CHAT_ID, "Auto-logged " + str(len(scan_result["logged"])) + " receipt(s) from email: " + vendors, parse_mode="")
                except Exception as se:
                    print("Email scan error: " + str(se))
                briefing = get_full_briefing()
                send_message(OWNER_CHAT_ID, briefing, parse_mode="")
                last_sent = date
            except Exception as e:
                print("Morning briefing error: " + str(e))
        time.sleep(60)

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
        if text_l in ("/start", "/help", "help"):
            send_message(chat_id,
                "*Primo Personal Assistant*\n\n"
                "*Calendar:*\n"
                "/today /week /schedule [event]\n\n"
                "*Email:*\n"
                "/emails /send [instruction]\n\n"
                "*Business:*\n"
                "/briefing /accounting /lesson\n\n"
                "*Search:*\n"
                "/search [topic] or just ask naturally\n\n"
                "*Sheet:*\n"
                "clear the sheet\n\n"
                "Or just type naturally!"
            )

        elif text_l in ("/briefing", "briefing", "update", "morning", "good morning"):
            send_message(chat_id, "Getting your briefing...", parse_mode="")
            send_message(chat_id, get_full_briefing(), parse_mode="")

        elif text_l in ("/lesson", "lesson", "business lesson", "teach me something"):
            send_message(chat_id, get_daily_lesson(), parse_mode="")

        elif text_l in ("/today", "today"):
            events = get_todays_events()
            if not events:
                send_message(chat_id, "Nothing on your calendar today!")
            else:
                lines = ["Today - " + datetime.date.today().strftime("%A, %B %d") + "\n"]
                for e in events:
                    lines.append(format_event(e))
                send_message(chat_id, "\n".join(lines), parse_mode="")

        elif text_l in ("/week", "week", "this week"):
            events = get_weeks_events()
            if not events:
                send_message(chat_id, "Nothing in your calendar this week!")
            else:
                lines = ["This Week:\n"]
                for e in events[:10]:
                    lines.append(format_event(e))
                send_message(chat_id, "\n".join(lines), parse_mode="")

        elif text_l.startswith("/schedule") or any(w in text_l for w in ["schedule","book","set up a meeting","add to calendar"]):
            instruction = text.replace("/schedule","").strip() or text
            send_message(chat_id, "Scheduling: " + instruction + "...", parse_mode="")
            parsed = parse_event_with_claude(instruction)
            dp     = parsed["date"].split("-")
            tp     = parsed["start_time"].split(":")
            start_dt = datetime.datetime(int(dp[0]),int(dp[1]),int(dp[2]),int(tp[0]),int(tp[1]))
            end_dt   = start_dt + datetime.timedelta(hours=float(parsed.get("duration_hours",1)))
            create_calendar_event(parsed["summary"], start_dt, end_dt, parsed.get("description",""))
            send_message(chat_id,
                "Event Created!\n\n" +
                parsed["summary"] + "\n" +
                start_dt.strftime("%A, %B %d at %I:%M %p") + "\n" +
                "Duration: " + str(parsed.get("duration_hours",1)) + " hour(s)",
                parse_mode=""
            )

        elif text_l in ("/emails", "emails", "check email", "unread", "inbox"):
            send_message(chat_id, "Checking your inbox...", parse_mode="")
            emails = get_unread_emails(5)
            if not emails:
                send_message(chat_id, "Inbox is clear!", parse_mode="")
            else:
                lines = [str(len(emails)) + " Unread Emails:\n"]
                for i, em in enumerate(emails, 1):
                    lines.append(str(i) + ". " + em["subject"][:50])
                    lines.append("   From: " + em["from"][:40])
                    lines.append("   " + em["snippet"][:80])
                    lines.append("")
                send_message(chat_id, "\n".join(lines), parse_mode="")

        elif text_l.startswith("/send") or any(w in text_l for w in ["send email","email to","write to","draft email"]):
            instruction = text.replace("/send","").strip() or text
            send_message(chat_id, "Drafting: " + instruction + "...", parse_mode="")
            draft = draft_email_with_claude(instruction)
            send_message(chat_id,
                "Email Draft:\n\nTo: " + draft["to"] +
                "\nSubject: " + draft["subject"] +
                "\n\n" + draft["body"][:300] +
                "\n\nReply confirm to send or cancel to discard.",
                parse_mode=""
            )
            app.pending_drafts = getattr(app, "pending_drafts", {})
            app.pending_drafts[chat_id] = draft

        elif text_l in ("confirm","yes send","send it") and hasattr(app,"pending_drafts") and chat_id in app.pending_drafts:
            draft = app.pending_drafts.pop(chat_id)
            send_email(draft["to"], draft["subject"], draft["body"])
            send_message(chat_id, "Email sent to " + draft["to"] + "!", parse_mode="")

        elif text_l in ("cancel","no","discard") and hasattr(app,"pending_drafts") and chat_id in app.pending_drafts:
            app.pending_drafts.pop(chat_id)
            send_message(chat_id, "Email discarded.", parse_mode="")

        elif text_l in ("/accounting", "accounting", "expenses", "spending"):
            if ACCOUNTING_API_URL:
                r    = requests.get(ACCOUNTING_API_URL + "/api/summary", timeout=10)
                data = r.json()
                if data.get("status") == "ok":
                    top  = "\n".join(["  * " + c["category"] + ": TTD " + str(round(c["amount"],2))
                                      for c in data.get("top_categories",[])])
                    send_message(chat_id,
                        "Accounting Summary\n\n"
                        "Total invoices: " + str(data.get("total_invoices",0)) + "\n"
                        "Pending: " + str(data.get("pending_invoices",0)) + "\n"
                        "This week: TTD " + str(round(data.get("total_spend_this_week",0),2)) + "\n"
                        "This month: TTD " + str(round(data.get("total_spend_this_month",0),2)) + "\n"
                        "All time: TTD " + str(round(data.get("total_spend_all_time",0),2)) + "\n\n"
                        "Top Categories:\n" + top,
                        parse_mode=""
                    )
            else:
                send_message(chat_id, "Accounting API not configured.", parse_mode="")

        elif text_l.startswith("/search") or text_l.startswith("search for") or text_l.startswith("search ") or text_l.startswith("look up") or text_l.startswith("find out"):
            query = text.replace("/search","").replace("search for","").replace("search","").replace("look up","").replace("find out","").strip()
            if not query:
                send_message(chat_id, "What would you like me to search for?", parse_mode="")
            else:
                send_message(chat_id, "Searching for: " + query + "...", parse_mode="")
                result = web_search(query)
                # Use Claude to format the result nicely
                resp = claude.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=300,
                    messages=[{"role":"user","content":
                        "User searched: " + query + ". Results: " + result + ". Summarise in 3-5 sentences."
                    }]
                )
                send_message(chat_id, resp.content[0].text, parse_mode="")

        elif text_l.startswith("/search") or "search for" in text_l or "look up" in text_l:
            query = text_l.replace("/search","").replace("search for","").replace("look up","").strip()
            if not query:
                send_message(chat_id, "What would you like me to search for?", parse_mode="")
            else:
                send_message(chat_id, "Searching...", parse_mode="")
                result = web_search(query)
                resp = claude.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=300,
                    messages=[{"role":"user","content":
                        "User searched: " + query + ". Results: " + result +
                        ". Summarise clearly in 3-5 sentences."
                    }]
                )
                send_message(chat_id, resp.content[0].text, parse_mode="")

        elif any(p in text_l for p in ["scan emails","check emails for receipts","scan for receipts",
                                        "find receipts","check for invoices","/scanemails"]):
            days = 10
            for word in text_l.split():
                if word.isdigit():
                    days = int(word)
                    break
            send_message(chat_id, "Scanning your emails for receipts from the last " + str(days) + " days...", parse_mode="")
            try:
                result = scan_and_log_receipts(days)
                logged  = result["logged"]
                if not logged:
                    send_message(chat_id, "No new receipts found in the last " + str(days) + " days. " + str(result["total_scanned"]) + " emails scanned.", parse_mode="")
                else:
                    lines = ["Found and logged " + str(len(logged)) + " receipts:", ""]
                    for item in logged:
                        lines.append(item["inv_id"] + " | " + item["vendor"] + " | TTD " + str(item["total"]))
                    lines.append("")
                    lines.append("All added to your Google Sheet!")
                    send_message(chat_id, " | ".join(lines), parse_mode="")
            except Exception as e:
                send_message(chat_id, "Error scanning emails: " + str(e)[:100], parse_mode="")

        elif any(p in text_l for p in ["clear the sheet","clear sheet","delete all invoices",
                                        "delete all entries","start fresh","start over",
                                        "wipe the sheet","reset the sheet","clear all data"]):
            send_message(chat_id, "Clearing all invoice data now...", parse_mode="")
            try:
                r    = requests.post(ACCOUNTING_API_URL + "/api/clearsheet", timeout=15)
                data = r.json()
                if data.get("status") == "ok":
                    send_message(chat_id, "All sheets cleared! Ready for a fresh start. Next invoice will be INV-001.", parse_mode="")
                else:
                    send_message(chat_id, "Error: " + data.get("message","unknown"), parse_mode="")
            except Exception as e:
                send_message(chat_id, "Error: " + str(e)[:100], parse_mode="")

        else:
            # Natural language — fetch accounting data for context
            acct_ctx = get_accounting_context()
            response = claude.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=400,
                messages=[{"role":"user","content":
                    "You are George's personal business assistant in Trinidad. "
                    "The user said: " + text +
                    acct_ctx +
                    "\n\nAnswer directly using the data. Use TTD for currency. "
                    "Do partial vendor name matching. Never say you don't have access to data. "
                    "Be concise."
                }]
            )
            send_message(chat_id, response.content[0].text, parse_mode="")

    except Exception as e:
        traceback.print_exc()
        send_message(chat_id, "Error: " + str(e)[:100] + "\n\nTry /help", parse_mode="")

    return "ok"

@app.route("/", methods=["GET"])
def health():
    return {"status":"ok","bot":"PrimoAssistanttBot","time":str(datetime.datetime.now())}

threading.Thread(target=morning_scheduler, daemon=True).start()

if __name__ == "__main__":
    print("PrimoAssistanttBot starting on http://localhost:5002")
    app.run(host="0.0.0.0", port=5002, debug=False)

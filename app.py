"""
WhatsApp Accounting Agent — Full Version with SendGrid
"""
import os, json, base64, datetime, requests, traceback
from flask import Flask, request
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
import gspread
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import threading, time
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

app = Flask(__name__)

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TWILIO_SID        = os.environ["TWILIO_SID"]
TWILIO_TOKEN      = os.environ["TWILIO_TOKEN"]
TWILIO_WA_NUM     = os.environ.get("TWILIO_WHATSAPP_NUM", "whatsapp:+14155238886")
SHEET_ID          = os.environ.get("SHEET_ID", "1mity1H5znYDITK9QLYORYD-UGt689LmY-fS29m13VLE")
GOOGLE_TOKEN_JSON = os.environ.get("GOOGLE_TOKEN_JSON", "")
REPORT_EMAIL      = os.environ.get("REPORT_EMAIL", "georgejgsolomon@gmail.com")
SENDGRID_API_KEY  = os.environ.get("SENDGRID_API_KEY", "")
SENDGRID_FROM     = os.environ.get("SENDGRID_FROM", "georgejgsolomon@gmail.com")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
twilio = Client(TWILIO_SID, TWILIO_TOKEN)

EXTRACT_PROMPT = """
You are an accounting AI. Analyze this invoice/bill image and extract fields.
Return ONLY valid JSON — no markdown, no explanation.
{
  "vendor":         "Company name on the invoice",
  "invoice_date":   "YYYY-MM-DD",
  "invoice_number": "Invoice reference number or N/A",
  "description":    "Brief description (max 40 chars)",
  "category":       "One of: Office Supplies | Software/Cloud | Shipping | Facilities | Marketing | Travel | Utilities | Professional Services | Food & Entertainment | Other",
  "currency":       "USD",
  "amount":         123.45,
  "tax":            12.34,
  "confidence":     "high | medium | low"
}
If a numeric field cannot be read use 0. If text cannot be read use Unknown.
"""

SHEET_HEADERS = ["ID","Date Received","Invoice Date","Invoice #","Vendor",
                 "Description","Category","Currency","Amount","Tax","Total","Status","WA Msg ID"]

def get_gspread_client():
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
    if GOOGLE_TOKEN_JSON:
        token_data = json.loads(GOOGLE_TOKEN_JSON)
    else:
        with open("token.json") as f:
            token_data = json.load(f)
    creds = Credentials.from_authorized_user_info(token_data, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return gspread.authorize(creds)

def get_or_create_sheet(sh, title, headers):
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1000, cols=len(headers))
        ws.append_row(headers)
    if ws.row_values(1) != headers:
        ws.insert_row(headers, 1)
    return ws

def get_all_invoices(sh):
    ws = get_or_create_sheet(sh, "Invoice Log", SHEET_HEADERS)
    return ws.get_all_records()

def refresh_summaries(sh):
    rows = get_all_invoices(sh)
    if not rows:
        return

    cat_data = {}
    for r in rows:
        cat = r.get("Category", "Other") or "Other"
        amt = float(r.get("Amount", 0) or 0)
        tax = float(r.get("Tax", 0) or 0)
        if cat not in cat_data:
            cat_data[cat] = {"count": 0, "amount": 0, "tax": 0}
        cat_data[cat]["count"]  += 1
        cat_data[cat]["amount"] += amt
        cat_data[cat]["tax"]    += tax

    total_spend = sum(v["amount"] for v in cat_data.values()) or 1
    cat_headers = ["Category","Invoice Count","Subtotal","Tax","Total","% of Spend"]
    ws_cat = get_or_create_sheet(sh, "Category Summary", cat_headers)
    ws_cat.clear()
    ws_cat.append_row(cat_headers)
    for cat, v in sorted(cat_data.items(), key=lambda x: -x[1]["amount"]):
        total = v["amount"] + v["tax"]
        pct   = round((v["amount"] / total_spend) * 100, 1)
        ws_cat.append_row([cat, v["count"], round(v["amount"],2),
                           round(v["tax"],2), round(total,2), f"{pct}%"])

    month_data = {}
    for r in rows:
        date_str = r.get("Invoice Date","") or r.get("Date Received","")
        try:
            month = str(date_str)[:7]
        except:
            month = "Unknown"
        amt = float(r.get("Amount", 0) or 0)
        tax = float(r.get("Tax", 0) or 0)
        if month not in month_data:
            month_data[month] = {"count": 0, "amount": 0, "tax": 0}
        month_data[month]["count"]  += 1
        month_data[month]["amount"] += amt
        month_data[month]["tax"]    += tax

    month_headers = ["Month","Invoice Count","Subtotal","Tax","Total"]
    ws_month = get_or_create_sheet(sh, "Monthly Summary", month_headers)
    ws_month.clear()
    ws_month.append_row(month_headers)
    for month, v in sorted(month_data.items()):
        ws_month.append_row([month, v["count"], round(v["amount"],2),
                             round(v["tax"],2), round(v["amount"]+v["tax"],2)])

def append_to_sheet(data, wa_msg_id):
    gc     = get_gspread_client()
    sh     = gc.open_by_key(SHEET_ID)
    ws     = get_or_create_sheet(sh, "Invoice Log", SHEET_HEADERS)
    rows   = ws.get_all_values()
    inv_id = f"INV-{len(rows):03d}"
    today  = datetime.date.today().strftime("%Y-%m-%d")
    amount = float(data.get("amount", 0))
    tax    = float(data.get("tax", 0))
    row = [
        inv_id, today,
        data.get("invoice_date", today),
        data.get("invoice_number", "N/A"),
        data.get("vendor", "Unknown"),
        data.get("description", ""),
        data.get("category", "Other"),
        data.get("currency", "USD"),
        amount, tax,
        round(amount + tax, 2),
        "Pending",
        wa_msg_id
    ]
    ws.append_row(row)
    refresh_summaries(sh)
    return inv_id

def build_summary_text(rows, period_label="All Time"):
    if not rows:
        return f"No invoices found for {period_label}."
    total_amt  = sum(float(r.get("Amount",0) or 0) for r in rows)
    total_tax  = sum(float(r.get("Tax",0) or 0) for r in rows)
    total      = total_amt + total_tax
    cat_totals = {}
    for r in rows:
        cat = r.get("Category","Other") or "Other"
        cat_totals[cat] = cat_totals.get(cat,0) + float(r.get("Amount",0) or 0)
    top_cats = sorted(cat_totals.items(), key=lambda x: -x[1])[:5]
    lines = [
        f"📊 *{period_label} Summary*",
        f"📋 Invoices: {len(rows)}",
        f"💵 Subtotal: ${total_amt:,.2f}",
        f"🧾 Tax: ${total_tax:,.2f}",
        f"💰 Total: ${total:,.2f}",
        "",
        "📂 *Top Categories:*"
    ]
    for cat, amt in top_cats:
        lines.append(f"  • {cat}: ${amt:,.2f}")
    return "\n".join(lines)

def get_weekly_rows(rows):
    today    = datetime.date.today()
    week_ago = today - datetime.timedelta(days=7)
    result   = []
    for r in rows:
        try:
            d = datetime.date.fromisoformat(str(r.get("Date Received",""))[:10])
            if d >= week_ago:
                result.append(r)
        except:
            pass
    return result

def get_monthly_rows(rows):
    month = datetime.date.today().strftime("%Y-%m")
    return [r for r in rows if str(r.get("Date Received","")).startswith(month)]

def send_weekly_email(rows):
    if not SENDGRID_API_KEY:
        print("SendGrid not configured — skipping email")
        return

    weekly      = get_weekly_rows(rows)
    monthly     = get_monthly_rows(rows)
    week_total  = sum(float(r.get("Amount",0) or 0)+float(r.get("Tax",0) or 0) for r in weekly)
    month_total = sum(float(r.get("Amount",0) or 0)+float(r.get("Tax",0) or 0) for r in monthly)

    rows_html = ""
    for i, r in enumerate(weekly):
        bg  = "#F5F5FC" if i%2==0 else "#EAEAF5"
        amt = float(r.get("Amount",0) or 0) + float(r.get("Tax",0) or 0)
        rows_html += (
            f'<tr style="background:{bg}">'
            f'<td style="padding:8px">{r.get("Vendor","")}</td>'
            f'<td style="padding:8px">{r.get("Category","")}</td>'
            f'<td style="padding:8px">{r.get("Date Received","")}</td>'
            f'<td style="padding:8px">${amt:,.2f}</td></tr>'
        )

    # Category breakdown
    cat_totals = {}
    for r in rows:
        cat = r.get("Category","Other") or "Other"
        cat_totals[cat] = cat_totals.get(cat,0) + float(r.get("Amount",0) or 0)
    cat_rows = ""
    for cat, amt in sorted(cat_totals.items(), key=lambda x: -x[1]):
        cat_rows += f'<tr><td style="padding:6px">{cat}</td><td style="padding:6px">${amt:,.2f}</td></tr>'

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:620px;margin:auto;padding:20px;color:#222">
    <div style="background:#0A0A1A;padding:24px;border-radius:8px;margin-bottom:24px">
      <h2 style="color:#00FFB3;margin:0">📊 Weekly Accounting Report</h2>
      <p style="color:#888;margin:8px 0 0">{datetime.date.today().strftime('%B %d, %Y')} · Accounting Agent</p>
    </div>

    <h3>🗓 This Week ({len(weekly)} invoices)</h3>
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;border:1px solid #ddd">
    <tr style="background:#1A1A3E;color:white">
      <th style="padding:10px;text-align:left">Vendor</th>
      <th style="padding:10px;text-align:left">Category</th>
      <th style="padding:10px;text-align:left">Date</th>
      <th style="padding:10px;text-align:left">Total</th>
    </tr>
    {rows_html if rows_html else '<tr><td colspan="4" style="padding:10px;color:#888">No invoices this week</td></tr>'}
    </table>
    <p style="font-size:16px"><strong>Week Total: ${week_total:,.2f}</strong></p>

    <hr style="border:1px solid #eee">
    <h3>📅 Month to Date</h3>
    <p>Invoices logged: <strong>{len(monthly)}</strong> &nbsp;|&nbsp; Total spend: <strong>${month_total:,.2f}</strong></p>

    <hr style="border:1px solid #eee">
    <h3>📂 All Time by Category</h3>
    <table width="60%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;border:1px solid #ddd">
    <tr style="background:#1A1A3E;color:white">
      <th style="padding:8px;text-align:left">Category</th>
      <th style="padding:8px;text-align:left">Total</th>
    </tr>
    {cat_rows}
    </table>

    <hr style="border:1px solid #eee">
    <p style="color:#aaa;font-size:12px">Sent automatically every Monday 8am · Accounting Agent</p>
    </body></html>
    """

    message = Mail(
        from_email=SENDGRID_FROM,
        to_emails=REPORT_EMAIL,
        subject=f"📊 Weekly Accounting Report — {datetime.date.today().strftime('%b %d, %Y')}",
        html_content=html
    )
    sg = SendGridAPIClient(SENDGRID_API_KEY)
    sg.send(message)
    print(f"Report sent to {REPORT_EMAIL}")

def weekly_scheduler():
    while True:
        now = datetime.datetime.utcnow()
        if now.weekday() == 0 and now.hour == 12 and now.minute < 5:
            try:
                gc   = get_gspread_client()
                sh   = gc.open_by_key(SHEET_ID)
                rows = get_all_invoices(sh)
                send_weekly_email(rows)
            except Exception as e:
                print(f"Scheduler error: {e}")
            time.sleep(360)
        time.sleep(60)

def fetch_image_b64(media_url):
    resp = requests.get(media_url, auth=(TWILIO_SID, TWILIO_TOKEN), timeout=20)
    resp.raise_for_status()
    media_type = resp.headers.get("Content-Type","image/jpeg").split(";")[0]
    return base64.standard_b64encode(resp.content).decode("utf-8"), media_type

def extract_invoice(image_b64, media_type):
    message = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=512,
        messages=[{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":media_type,"data":image_b64}},
            {"type":"text","text":EXTRACT_PROMPT}
        ]}]
    )
    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)

@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    num_media = int(request.form.get("NumMedia", 0))
    msg_sid   = request.form.get("MessageSid", "unknown")
    body_text = request.form.get("Body", "").strip().lower()
    resp      = MessagingResponse()

    if num_media == 0:
        try:
            gc   = get_gspread_client()
            sh   = gc.open_by_key(SHEET_ID)
            rows = get_all_invoices(sh)
        except:
            rows = []

        if "summary" in body_text or "report" in body_text:
            resp.message(build_summary_text(rows, "All Time"))
        elif "week" in body_text:
            resp.message(build_summary_text(get_weekly_rows(rows), "This Week"))
        elif "month" in body_text:
            resp.message(build_summary_text(get_monthly_rows(rows), "This Month"))
        elif "categor" in body_text:
            cat_totals = {}
            for r in rows:
                cat = r.get("Category","Other") or "Other"
                cat_totals[cat] = cat_totals.get(cat,0) + float(r.get("Amount",0) or 0)
            lines = ["📂 *Spend by Category (All Time)*\n"]
            for cat, amt in sorted(cat_totals.items(), key=lambda x: -x[1]):
                lines.append(f"• {cat}: ${amt:,.2f}")
            resp.message("\n".join(lines))
        else:
            resp.message(
                "👋 *Accounting Agent*\n\n"
                "📸 Send a photo of any bill to log it\n\n"
                "*Commands:*\n"
                "• *summary* — all time totals\n"
                "• *week* — this week\n"
                "• *month* — this month\n"
                "• *categories* — spend by category"
            )
        return str(resp)

    try:
        media_url = request.form.get("MediaUrl0")
        image_b64, media_type = fetch_image_b64(media_url)
        data   = extract_invoice(image_b64, media_type)
        inv_id = append_to_sheet(data, msg_sid)

        conf_emoji = {"high":"✅","medium":"⚠️","low":"🔴"}.get(data.get("confidence","?"),"❓")
        amount = float(data.get("amount",0))
        tax    = float(data.get("tax",0))

        resp.message(
            f"✅ *Invoice Logged!*\n\n"
            f"🆔 ID: `{inv_id}`\n"
            f"🏢 Vendor: {data.get('vendor','Unknown')}\n"
            f"📅 Date: {data.get('invoice_date','N/A')}\n"
            f"🔢 Invoice #: {data.get('invoice_number','N/A')}\n"
            f"📦 Category: {data.get('category','Other')}\n"
            f"💵 Amount: {data.get('currency','USD')} {amount:,.2f}\n"
            f"🧾 Tax: {data.get('currency','USD')} {tax:,.2f}\n"
            f"💰 Total: {data.get('currency','USD')} {amount+tax:,.2f}\n\n"
            f"{conf_emoji} Confidence: {data.get('confidence','?')}\n"
            f"📊 Google Sheet updated!"
        )

    except json.JSONDecodeError:
        resp.message("⚠️ Could read image but had trouble parsing details. Try a clearer photo.")
    except Exception as e:
        traceback.print_exc()
        resp.message(f"❌ Something went wrong. Error: {str(e)[:80]}")

    return str(resp)

@app.route("/send-report", methods=["GET"])
def manual_report():
    try:
        gc   = get_gspread_client()
        sh   = gc.open_by_key(SHEET_ID)
        rows = get_all_invoices(sh)
        send_weekly_email(rows)
        return {"status": "Report sent!", "to": REPORT_EMAIL}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.route("/", methods=["GET"])
def health():
    return {"status":"ok","agent":"Accounting Agent v2","time":str(datetime.datetime.now())}

scheduler_thread = threading.Thread(target=weekly_scheduler, daemon=True)
scheduler_thread.start()

if __name__ == "__main__":
    print("Accounting Agent v2 starting on http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)

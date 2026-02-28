# ============================================================
# Peyton & Charmed - LINE OA Router Server
# Routes messages to BOTH Zoho (unchanged) AND Claude (พี่เจนนี่)
# ============================================================
# Updated: PERSISTENT form tracking (survives server restarts)

import os
import json
import hashlib
import hmac
import base64
import logging
from collections import defaultdict
from datetime import datetime, timedelta

import requests
from flask import Flask, request, abort
import anthropic

from system_prompt import (
    SYSTEM_PROMPT_MODE_A,
    SYSTEM_PROMPT_MODE_B,
    ZOHO_FORM_BASE_URL,
)

# ============================================================
# CONFIGURATION (Set these in Render Environment Variables)
# ============================================================
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ZOHO_WEBHOOK_URL = os.environ.get("ZOHO_WEBHOOK_URL", "")

# Team notifications via email
TEAM_EMAIL_ADDRESSES = os.environ.get("TEAM_EMAIL_ADDRESSES", "")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "")
SENDER_PASSWORD = os.environ.get("SENDER_PASSWORD", "")
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = os.environ.get("SMTP_PORT", "587")

# Optional: Set to "true" to disable Claude replies (forwarding only)
FORWARDING_ONLY = os.environ.get("FORWARDING_ONLY", "false").lower() == "true"

# ============================================================
# SETUP
# ============================================================
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ============================================================
# CONVERSATION MEMORY
# Stores recent messages per user for natural conversation flow
# (This can be in-memory since it's just for current chat context)
# ============================================================
conversation_history = defaultdict(list)
MAX_HISTORY = 10

# ============================================================
# PERSISTENT FORM TRACKING (SAVED TO FILE)
# This is the KEY FIX - data survives server restarts!
# ============================================================
FORM_DATA_FILE = "/opt/render/project/src/form_tracking.json"

def load_form_data():
    """Load form tracking data from file."""
    try:
        if os.path.exists(FORM_DATA_FILE):
            with open(FORM_DATA_FILE, "r") as f:
                data = json.load(f)
                logger.info(f"Loaded form data: {len(data.get('completed', []))} completed, {len(data.get('link_sent', []))} link sent")
                return data
    except Exception as e:
        logger.error(f"Error loading form data: {e}")
    
    # Return empty data if file doesn't exist or has errors
    return {"completed": [], "link_sent": []}

def save_form_data():
    """Save form tracking data to file."""
    try:
        data = {
            "completed": list(form_completed_users),
            "link_sent": list(form_link_sent_users),
        }
        with open(FORM_DATA_FILE, "w") as f:
            json.dump(data, f)
        logger.info(f"Saved form data: {len(form_completed_users)} completed, {len(form_link_sent_users)} link sent")
    except Exception as e:
        logger.error(f"Error saving form data: {e}")

# Load saved data when server starts
_saved_data = load_form_data()
form_completed_users = set(_saved_data.get("completed", []))
form_link_sent_users = set(_saved_data.get("link_sent", []))
logger.info(f"Restored: {len(form_completed_users)} completed users, {len(form_link_sent_users)} link-sent users")

def mark_form_completed(user_id):
    """Mark a user as having completed the form (and save to file)."""
    form_completed_users.add(user_id)
    save_form_data()
    logger.info(f"User {user_id} marked as form completed (saved)")

def has_form_been_completed(user_id):
    """Check if user has told us they completed the form."""
    return user_id in form_completed_users

def mark_form_link_sent(user_id):
    """Mark that we already sent the form link to this user (and save to file)."""
    form_link_sent_users.add(user_id)
    save_form_data()

def has_form_link_been_sent(user_id):
    """Check if we already sent the form link to this user."""
    return user_id in form_link_sent_users

def check_if_user_says_form_done(user_message):
    """
    Check if the user's message means they completed the form.
    Returns True if they say something like 'done', 'completed', etc.
    """
    done_phrases = [
        # Thai phrases
        "กรอกแล้ว",
        "กรอกเรียบร้อย",
        "เรียบร้อยแล้ว",
        "เรียบร้อยค่ะ",
        "เรียบร้อยครับ",
        "กรอกเสร็จ",
        "ส่งแล้ว",
        "ส่งฟอร์มแล้ว",
        "ทำแล้ว",
        "ทำเสร็จแล้ว",
        "เสร็จแล้ว",
        "เสร็จแล้วค่ะ",
        "เสร็จแล้วครับ",
        "กรอกเรียบร้อยค่ะ",
        "กรอกเรียบร้อยครับ",
        "กรอกเรียบร้อยค่า",
        "กรอกเรียบร้อยคะ",
        "ส่งแล้วค่ะ",
        "ส่งแล้วครับ",
        "ส่งไปแล้ว",
        "ทำเรียบร้อย",
        "เรียบร้อย",
        "กรอกฟอร์มแล้ว",
        "กรอกฟอร์มเรียบร้อย",
        "กรอกฟอร์มเสร็จ",
        "กรอกแล้วครับ",
        "กรอกแล้วค่ะ",
        # English phrases
        "done",
        "completed",
        "finished",
        "submitted",
        "filled out",
        "filled in",
        "already filled",
        "already done",
        "already completed",
        "i filled",
        "i completed",
        "form done",
        "form completed",
        "form submitted",
    ]

    message_lower = user_message.lower().strip()

    for phrase in done_phrases:
        if phrase in message_lower:
            return True

    return False

# ============================================================
# CONVERSATION HISTORY FUNCTIONS
# ============================================================
def add_to_history(user_id, role, content):
    """Add a message to conversation history."""
    conversation_history[user_id].append({
        "role": role,
        "content": content,
        "timestamp": datetime.now().isoformat()
    })
    if len(conversation_history[user_id]) > MAX_HISTORY:
        conversation_history[user_id] = conversation_history[user_id][-MAX_HISTORY:]

def get_history(user_id):
    """Get conversation history formatted for Claude API."""
    messages = []
    for msg in conversation_history[user_id]:
        messages.append({
            "role": msg["role"],
            "content": msg["content"]
        })
    return messages

def clean_old_histories():
    """Remove conversation histories older than 24 hours.
    NOTE: We do NOT remove form tracking data anymore!
    Form tracking is permanent so returning customers are remembered.
    """
    cutoff = datetime.now() - timedelta(hours=24)
    users_to_remove = []
    for user_id, messages in conversation_history.items():
        if messages and datetime.fromisoformat(messages[-1]["timestamp"]) < cutoff:
            users_to_remove.append(user_id)
    for user_id in users_to_remove:
        del conversation_history[user_id]
        # DO NOT remove form tracking - we want to remember customers forever!

# ============================================================
# LINE SIGNATURE VERIFICATION
# ============================================================
def verify_signature(body, signature):
    """Verify that the request is from LINE."""
    hash_value = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256
    ).digest()
    expected_signature = base64.b64encode(hash_value).decode("utf-8")
    return hmac.compare_digest(signature, expected_signature)

# ============================================================
# ZOHO FORWARDING
# ============================================================
def forward_to_zoho(body, headers):
    """Forward the raw LINE webhook data to Zoho."""
    if not ZOHO_WEBHOOK_URL:
        logger.warning("ZOHO_WEBHOOK_URL not set, skipping Zoho forwarding")
        return

    try:
        forward_headers = {
            "Content-Type": headers.get("Content-Type", "application/json"),
            "X-Line-Signature": headers.get("X-Line-Signature", ""),
        }
        response = requests.post(
            ZOHO_WEBHOOK_URL,
            data=body,
            headers=forward_headers,
            timeout=10
        )
        logger.info(f"Forwarded to Zoho: status {response.status_code}")
    except Exception as e:
        logger.error(f"Failed to forward to Zoho: {e}")

# ============================================================
# LINE REPLY
# ============================================================
def reply_to_line(reply_token, text):
    """Send a reply message to LINE."""
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }
    data = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}]
    }
    try:
        response = requests.post(url, headers=headers, json=data, timeout=10)
        logger.info(f"LINE reply: status {response.status_code}")
        if response.status_code != 200:
            logger.error(f"LINE reply error: {response.text}")
    except Exception as e:
        logger.error(f"Failed to reply on LINE: {e}")

# ============================================================
# TEAM NOTIFICATION (EMAIL)
# ============================================================
def send_team_notification(user_message, handoff_reason):
    """Send email notification to team when handoff is triggered."""

    team_emails = os.environ.get("TEAM_EMAIL_ADDRESSES", "").split(",")
    team_emails = [email.strip() for email in team_emails if email.strip()]

    if not team_emails:
        logger.warning("TEAM_EMAIL_ADDRESSES not set, skipping team notification")
        return

    topic_subjects = {
        "booking": "🏠 New Booking Request",
        "payment": "💳 Payment Inquiry",
        "contract": "📄 Contract Question",
        "visa": "📘 Visa Support Request",
        "customer_needs_help": "❓ Customer Needs Help",
        "unknown": "❓ Customer Needs Help"
    }

    subject = topic_subjects.get(handoff_reason, "❓ Customer Needs Help")

    if len(user_message) > 200:
        user_message = user_message[:200] + "..."

    email_body = f"""
A customer needs team assistance:

Topic: {handoff_reason.title()}
Customer Message: "{user_message}"

Please check Zoho CRM for full customer details and follow up accordingly.

Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

---
Peyton & Charmed Bot Alert System
"""

    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        sender_email = os.environ.get("SENDER_EMAIL", "")
        sender_password = os.environ.get("SENDER_PASSWORD", "")

        if not sender_email or not sender_password:
            logger.warning("Email credentials not configured, skipping notification")
            return

        message = MIMEMultipart()
        message["From"] = sender_email
        message["To"] = ", ".join(team_emails)
        message["Subject"] = subject
        message.attach(MIMEText(email_body, "plain"))

        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        text = message.as_string()
        server.sendmail(sender_email, team_emails, text)
        server.quit()

        logger.info(f"Team notification email sent to {len(team_emails)} recipients")

    except Exception as e:
        logger.error(f"Failed to send team notification email: {e}")

# ============================================================
# HANDOFF DETECTION & CLEANUP
# ============================================================
def detect_handoff_trigger(bot_reply):
    """Detect if bot reply contains [HANDOFF] tag."""
    return "[HANDOFF]" in bot_reply

def strip_handoff_tag(bot_reply):
    """Remove [HANDOFF] tag before sending to customer."""
    return bot_reply.replace("[HANDOFF]", "").strip()

# ============================================================
# CLAUDE (พี่เจนนี่) - Get AI Response
# ============================================================
def get_jenny_reply(user_id, user_message, form_completed=False):
    """Get a reply from Claude as พี่เจนนี่."""

    # Choose system prompt based on form status
    if form_completed:
        system_prompt = SYSTEM_PROMPT_MODE_B
    else:
        form_link = f"{ZOHO_FORM_BASE_URL}?Line_ID={user_id}"
        system_prompt = SYSTEM_PROMPT_MODE_A.replace("{form_link}", form_link)

    # Add user message to history
    add_to_history(user_id, "user", user_message)

    # Get conversation history
    messages = get_history(user_id)

    try:
        response = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            system=system_prompt,
            messages=messages
        )
        reply = response.content[0].text

        # Add assistant reply to history
        add_to_history(user_id, "assistant", reply)

        return reply

    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return "ขอโทษนะคะ ระบบมีปัญหาทางเทคนิคค่ะ ทีมจะติดต่อกลับเร็วๆ นี้นะคะ 🙏 [HANDOFF]"

# ============================================================
# MAIN WEBHOOK ENDPOINT
# ============================================================
@app.route("/callback", methods=["POST"])
def callback():
    """Main webhook endpoint - receives all LINE events."""

    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    if not verify_signature(body, signature):
        logger.warning("Invalid signature")
        abort(400)

    # STEP 1: Forward EVERYTHING to Zoho
    forward_to_zoho(body, dict(request.headers))

    # STEP 2: Process with Claude if applicable
    if FORWARDING_ONLY:
        logger.info("Forwarding only mode - skipping Claude")
        return "OK"

    try:
        events = json.loads(body).get("events", [])
    except json.JSONDecodeError:
        logger.error("Invalid JSON body")
        return "OK"

    for event in events:
        if event.get("type") != "message":
            continue

        reply_token = event.get("replyToken", "")
        user_id = event.get("source", {}).get("userId", "")
        message = event.get("message", {})
        message_type = message.get("type", "")

        if not reply_token or not user_id:
            continue

        # Clean old conversation histories periodically
        clean_old_histories()

        # ============================================================
        # CHECK FORM STATUS (now reads from PERSISTENT file storage)
        # ============================================================
        form_completed = has_form_been_completed(user_id)

        if message_type == "text":
            user_text = message.get("text", "")
            logger.info(f"User {user_id}: {user_text[:50]}...")

            # ============================================================
            # CHECK: Did the user just say they completed the form?
            # ============================================================
            if not form_completed and check_if_user_says_form_done(user_text):
                mark_form_completed(user_id)
                form_completed = True
                logger.info(f"User {user_id} says form is completed!")

                # Send a nice thank you and let them know team will follow up
                reply = (
                    "ขอบคุณน้องมากค่ะ 😊\n\n"
                    "ทีมงานได้รับข้อมูลจากแบบฟอร์มแล้วค่ะ "
                    "จะมีทีมติดต่อกลับไปให้น้องเร็วๆ นี้เลยค่ะ "
                    "พร้อมกับแนะนำตัวเลือกที่พักที่เหมาะกับความต้องการของน้องโดยเฉพาะเลยนะคะ\n\n"
                    "รอติดต่อกลับไปนะคะ ขอบคุณค่ะ"
                )

                # Strip any accidental HANDOFF tags
                clean_reply = strip_handoff_tag(reply)
                reply_to_line(reply_token, clean_reply)

                # Notify team that form was completed
                send_team_notification(user_text, "customer_needs_help")
                continue

            # Regular text message - get Claude reply
            reply = get_jenny_reply(user_id, user_text, form_completed)

            # If Claude's reply contains the form link, mark it as sent
            if ZOHO_FORM_BASE_URL in reply:
                mark_form_link_sent(user_id)

            # Check if this reply triggers a handoff to team
            if detect_handoff_trigger(reply):
                logger.info(f"Handoff triggered for user {user_id}")
                send_team_notification(user_text, "customer_needs_help")

            # ALWAYS strip [HANDOFF] tag before sending to customer
            clean_reply = strip_handoff_tag(reply)
            reply_to_line(reply_token, clean_reply)

        elif message_type == "sticker":
            # Ignore stickers - no reply needed
            logger.info(f"Sticker from {user_id} - ignoring")
            continue

        elif message_type == "image":
            if form_completed:
                reply = "ได้รับรูปแล้วค่ะ 😊 มีอะไรให้ช่วยดูไหมคะ?"
            elif has_form_link_been_sent(user_id):
                reply = "ได้รับรูปแล้วค่ะ 😊 กรอกฟอร์มเสร็จแล้วบอกพี่ด้วยนะคะ"
            else:
                form_link = f"{ZOHO_FORM_BASE_URL}?Line_ID={user_id}"
                reply = (
                    f"ได้รับรูปแล้วค่ะ 😊 ทีมงานดูรูปไม่ได้ "
                    f"แต่ยินดีช่วยเหลือเรื่องที่พักนะคะ "
                    f"รบกวนกรอกฟอร์มนี้ให้เราก่อนนะคะ 👉 {form_link}"
                )
                mark_form_link_sent(user_id)
            reply_to_line(reply_token, reply)

        elif message_type in ("audio", "video", "file"):
            reply = (
                "ได้รับแล้วค่ะ 😊 ทีมงานตอบได้ทางข้อความนะคะ "
                "พิมพ์คำถามมาได้เลยค่ะ ยินดีช่วยเหลือค่ะ 💬"
            )
            reply_to_line(reply_token, reply)

        else:
            logger.info(f"Ignoring message type: {message_type}")

    return "OK"

# ============================================================
# HEALTH CHECK ENDPOINT
# ============================================================
@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint for monitoring."""
    return {
        "status": "ok",
        "bot": "Peyton & Charmed Team Bot",
        "forwarding": "active" if ZOHO_WEBHOOK_URL else "not configured",
        "claude": "active" if ANTHROPIC_API_KEY else "not configured",
        "email_notifications": "active" if TEAM_EMAIL_ADDRESSES and SENDER_EMAIL else "not configured",
        "mode": "forwarding_only" if FORWARDING_ONLY else "full",
        "form_completed_users": len(form_completed_users),
        "form_link_sent_users": len(form_link_sent_users)
    }

# ============================================================
# SAFETY MODE ENDPOINTS
# ============================================================
@app.route("/safety/forwarding-only", methods=["POST"])
def enable_forwarding_only():
    """Emergency: disable Claude replies, keep Zoho forwarding."""
    global FORWARDING_ONLY
    FORWARDING_ONLY = True
    return {"status": "forwarding_only_enabled", "claude_replies": "disabled"}

@app.route("/safety/full-mode", methods=["POST"])
def enable_full_mode():
    """Re-enable Claude replies."""
    global FORWARDING_ONLY
    FORWARDING_ONLY = False
    return {"status": "full_mode_enabled", "claude_replies": "enabled"}

# ============================================================
# RUN
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting Peyton & Charmed Bot on port {port}")
    logger.info(f"Zoho forwarding: {'active' if ZOHO_WEBHOOK_URL else 'NOT CONFIGURED'}")
    logger.info(f"Claude replies: {'disabled' if FORWARDING_ONLY else 'active'}")
    logger.info(f"Form tracking file: {FORM_DATA_FILE}")
    app.run(host="0.0.0.0", port=port)


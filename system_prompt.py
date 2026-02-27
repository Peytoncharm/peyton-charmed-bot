# ============================================================
# Peyton & Charmed - LINE OA Router Server
# Routes messages to BOTH Zoho (unchanged) AND Claude (พี่เจนนี่)
# ============================================================

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
TEAM_EMAIL_ADDRESSES = os.environ.get("TEAM_EMAIL_ADDRESSES", "")  # Comma-separated emails
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "")  # Gmail account for sending
SENDER_PASSWORD = os.environ.get("SENDER_PASSWORD", "")  # Gmail app password
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
# ============================================================
conversation_history = defaultdict(list)
MAX_HISTORY = 10  # Remember last 10 messages per user

def add_to_history(user_id, role, content):
    """Add a message to conversation history."""
    conversation_history[user_id].append({
        "role": role,
        "content": content,
        "timestamp": datetime.now().isoformat()
    })
    # Keep only the last MAX_HISTORY messages
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
    """Remove conversation histories older than 24 hours."""
    cutoff = datetime.now() - timedelta(hours=24)
    users_to_remove = []
    for user_id, messages in conversation_history.items():
        if messages and datetime.fromisoformat(messages[-1]["timestamp"]) < cutoff:
            users_to_remove.append(user_id)
    for user_id in users_to_remove:
        del conversation_history[user_id]

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
    """Forward the raw LINE webhook data to Zoho (keeps existing integration alive)."""
    if not ZOHO_WEBHOOK_URL:
        logger.warning("ZOHO_WEBHOOK_URL not set, skipping Zoho forwarding")
        return

    try:
        # Forward with relevant headers
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
        # Don't raise - we don't want Zoho issues to block LINE replies

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
    
    # Get team email addresses from environment variable
    team_emails = os.environ.get("TEAM_EMAIL_ADDRESSES", "").split(",")
    team_emails = [email.strip() for email in team_emails if email.strip()]
    
    if not team_emails:
        logger.warning("TEAM_EMAIL_ADDRESSES not set, skipping team notification")
        return

    # Determine topic based on handoff reason
    topic_subjects = {
        "booking": "🏠 New Booking Request",
        "payment": "💳 Payment Inquiry", 
        "contract": "📄 Contract Question",
        "visa": "📘 Visa Support Request",
        "unknown": "❓ Customer Needs Help"
    }
    
    subject = topic_subjects.get(handoff_reason, "❓ Customer Needs Help")
    
    # Truncate message if too long
    if len(user_message) > 200:
        user_message = user_message[:200] + "..."
    
    # Create email content
    email_body = f"""
A customer needs team assistance:

Topic: {handoff_reason.title()}
Customer Message: "{user_message}"

Please check Zoho CRM for full customer details and follow up accordingly.

Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

---
Peyton & Charmed Bot Alert System
"""

    # Send email via simple SMTP (using Gmail SMTP as example)
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        
        # Email configuration from environment variables
        smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        sender_email = os.environ.get("SENDER_EMAIL", "")
        sender_password = os.environ.get("SENDER_PASSWORD", "")
        
        if not sender_email or not sender_password:
            logger.warning("Email credentials not configured, skipping notification")
            return
        
        # Create message
        message = MIMEMultipart()
        message["From"] = sender_email
        message["To"] = ", ".join(team_emails)
        message["Subject"] = subject
        message.attach(MIMEText(email_body, "plain"))
        
        # Send email
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        text = message.as_string()
        server.sendmail(sender_email, team_emails, text)
        server.quit()
        
        logger.info(f"Team notification email sent to {len(team_emails)} recipients")
        
    except Exception as e:
        logger.error(f"Failed to send team notification email: {e}")

def detect_handoff_trigger(bot_reply):
    """
    Detect if bot reply contains [HANDOFF] tag.
    Returns True if handoff is needed, False otherwise.
    """
    return "[HANDOFF]" in bot_reply

def strip_handoff_tag(bot_reply):
    """
    Remove [HANDOFF] tag from bot reply before sending to customer.
    Customer should never see this tag.
    """
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
        # Insert the form link into Mode A prompt
        form_link = f"{ZOHO_FORM_BASE_URL}?Line_ID={user_id}"
        system_prompt = SYSTEM_PROMPT_MODE_A.replace("{form_link}", form_link)

    # Add user message to history
    add_to_history(user_id, "user", user_message)

    # Get conversation history
    messages = get_history(user_id)

    try:
        response = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,  # Keep replies concise for chat
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
# FORM STATUS CHECK
# ============================================================
def check_form_completed(user_id):
    """
    Check if this LINE user has completed the Zoho form.

    TODO: Replace this with actual Zoho CRM API call.
    For now, defaults to False (Mode A: Form Nudger).

    To implement:
    1. Set up Zoho CRM API OAuth2 credentials
    2. Search Zoho CRM contacts/leads by LINE_ID custom field
    3. Check if lead exists and has form_completed = true

    Example Zoho API call:
        GET https://www.zohoapis.com/crm/v2/Leads/search?criteria=(LINE_ID:equals:{user_id})
    """
    # ============================================================
    # PHASE 1: Always use Mode A (Form Nudger)
    # This is safe - พี่เจนนี่ will always nudge for form completion
    #
    # PHASE 2: Uncomment below to check Zoho CRM
    # ============================================================

    # PHASE 2 (uncomment when ready):
    # try:
    #     zoho_token = get_zoho_access_token()
    #     headers = {"Authorization": f"Zoho-oauthtoken {zoho_token}"}
    #     url = f"https://www.zohoapis.com/crm/v2/Leads/search?criteria=(LINE_ID:equals:{user_id})"
    #     response = requests.get(url, headers=headers, timeout=5)
    #     if response.status_code == 200:
    #         data = response.json()
    #         if data.get("data"):
    #             # Lead exists = form was completed
    #             return True
    #     return False
    # except Exception as e:
    #     logger.error(f"Zoho check failed: {e}")
    #     return False

    return False  # Default: assume form not completed (Mode A)

# ============================================================
# MAIN WEBHOOK ENDPOINT
# ============================================================
@app.route("/callback", methods=["POST"])
def callback():
    """Main webhook endpoint - receives all LINE events."""

    # Get request data
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    # Verify signature
    if not verify_signature(body, signature):
        logger.warning("Invalid signature")
        abort(400)

    # ============================================================
    # STEP 1: Forward EVERYTHING to Zoho (keeps existing flow alive)
    # ============================================================
    forward_to_zoho(body, dict(request.headers))

    # ============================================================
    # STEP 2: Process with Claude (พี่เจนนี่) if applicable
    # ============================================================
    if FORWARDING_ONLY:
        logger.info("Forwarding only mode - skipping Claude")
        return "OK"

    try:
        events = json.loads(body).get("events", [])
    except json.JSONDecodeError:
        logger.error("Invalid JSON body")
        return "OK"

    for event in events:
        # Only process message events from users
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

        # Check if form has been completed
        form_completed = check_form_completed(user_id)

        if message_type == "text":
            # Text message - get Claude reply
            user_text = message.get("text", "")
            logger.info(f"User {user_id}: {user_text[:50]}...")

            reply = get_jenny_reply(user_id, user_text, form_completed)
            
            # Check if this reply triggers a handoff to team
            if detect_handoff_trigger(reply):
                logger.info(f"Handoff triggered for user {user_id}")
                send_team_notification(user_text, "customer_needs_help")
            
            # Strip [HANDOFF] tag before sending to customer
            clean_reply = strip_handoff_tag(reply)
            reply_to_line(reply_token, clean_reply)

        elif message_type == "sticker":
            # Sticker - friendly response + nudge form if needed
            if form_completed:
                reply = "น่ารักค่ะ 😊 มีอะไรให้ช่วยไหมคะ?"
            else:
                form_link = f"{ZOHO_FORM_BASE_URL}?Line_ID={user_id}"
                reply = (
                    f"น่ารักค่ะ 😊 ทีมงานยินดีช่วยเหลือนะคะ "
                    f"กรอกฟอร์มสั้นๆ นี้ให้เราก่อนนะคะ "
                    f"จะได้แนะนำที่พักที่เหมาะกับน้องได้เลยค่ะ 👉 {form_link}"
                )
            reply_to_line(reply_token, reply)

        elif message_type == "image":
            # Image - acknowledge
            if form_completed:
                reply = "ได้รับรูปแล้วค่ะ 😊 มีอะไรให้ช่วยดูไหมคะ?"
            else:
                form_link = f"{ZOHO_FORM_BASE_URL}?Line_ID={user_id}"
                reply = (
                    f"ได้รับรูปแล้วค่ะ 😊 ทีมงานดูรูปไม่ได้ "
                    f"แต่ยินดีช่วยเหลือเรื่องที่พักนะคะ "
                    f"รบกวนกรอกฟอร์มนี้ให้เราก่อนนะคะ 👉 {form_link}"
                )
            reply_to_line(reply_token, reply)

        elif message_type in ("audio", "video", "file"):
            # Voice/Video/File - acknowledge
            reply = (
                "ได้รับแล้วค่ะ 😊 ทีมงานตอบได้ทางข้อความนะคะ "
                "พิมพ์คำถามมาได้เลยค่ะ ยินดีช่วยเหลือค่ะ 💬"
            )
            reply_to_line(reply_token, reply)

        else:
            # Unknown message type - ignore
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
        "mode": "forwarding_only" if FORWARDING_ONLY else "full"
    }

# ============================================================
# SAFETY MODE ENDPOINT
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
    app.run(host="0.0.0.0", port=port)

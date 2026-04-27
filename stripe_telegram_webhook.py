import os
import requests
import stripe
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

STRIPE_SECRET_KEY     = os.environ["STRIPE_SECRET_KEY"]
STRIPE_WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
TELEGRAM_BOT_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID      = os.environ["TELEGRAM_CHAT_ID"]

stripe.api_key = STRIPE_SECRET_KEY

app = Flask(__name__)


def send_telegram(message):
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, data=data, timeout=10)
        print(f"[Telegram] status={resp.status_code} response={resp.text}")
    except Exception as e:
        print(f"[Telegram] Erreur : {e}")


@app.route("/")
def index():
    return "Server is running", 200


@app.route("/webhook/stripe", methods=["POST"])
def webhook():
    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        print("[Stripe] Signature invalide")
        return jsonify({"error": "Invalid signature"}), 400

    print(f"[Stripe] Événement reçu : {event['type']}")

    if event["type"] == "checkout.session.completed":
        session  = event["data"]["object"]
        email    = (session.get("customer_details") or {}).get("email", "inconnu")
        montant  = (session.get("amount_total") or 0) / 100
        currency = (session.get("currency") or "eur").upper()

        message = (
            f"💸 <b>NOUVEAU PAIEMENT</b>\n"
            f"💰 Montant : {montant} {currency}\n"
            f"📧 Client : {email}"
        )
        send_telegram(message)

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)

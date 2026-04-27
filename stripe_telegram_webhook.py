import os
import requests
import stripe
from flask import Flask, request, jsonify, redirect
from dotenv import load_dotenv

load_dotenv()

STRIPE_SECRET_KEY     = os.environ["STRIPE_SECRET_KEY"]
STRIPE_WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
TELEGRAM_BOT_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID      = os.environ["TELEGRAM_CHAT_ID"]
TELEGRAM_INVITE_LINK  = os.environ["TELEGRAM_INVITE_LINK"]   # https://t.me/+LXhhEPh00pQxNTE0

stripe.api_key = STRIPE_SECRET_KEY

app = Flask(__name__)

# Sessions déjà traitées (évite les doublons)
_used_sessions = set()


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


@app.route("/success")
def success():
    session_id = request.args.get("session_id", "").strip()

    if not session_id:
        return "Lien invalide.", 400

    if session_id in _used_sessions:
        return "Ce lien a déjà été utilisé.", 403

    # Vérification du paiement auprès de Stripe
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        print(f"[Stripe] Erreur retrieve : {e}")
        return "Paiement introuvable.", 404

    if session.payment_status != "paid":
        return "Paiement non validé.", 402

    _used_sessions.add(session_id)
    print(f"[Success] Accès accordé pour session={session_id}")

    # Redirection vers le canal Telegram
    return redirect(TELEGRAM_INVITE_LINK)


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

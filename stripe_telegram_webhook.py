"""
Stripe → Telegram access gate
------------------------------
Flow :
  1. L'acheteur clique sur ton lien Stripe (buy.stripe.com/...)
  2. Après paiement, Stripe redirige vers /success?session_id={CHECKOUT_SESSION_ID}
  3. Le serveur vérifie le paiement via l'API Stripe
  4. Un lien Telegram à usage unique est généré et affiché

Configuration dans Stripe Dashboard :
  Payment Links → ton lien → After payment → Redirect to URL :
  https://ton-domaine.com/success?session_id={CHECKOUT_SESSION_ID}

Variables d'environnement (.env) :
  STRIPE_SECRET_KEY       sk_live_...
  STRIPE_WEBHOOK_SECRET   whsec_...   (optionnel, pour le webhook de secours)
  TELEGRAM_BOT_TOKEN      123456:ABC-...
  TELEGRAM_CHAT_ID        -100xxxxxxxxxx

Lancer :
  pip install flask stripe python-dotenv requests
  python stripe_telegram_webhook.py
"""

import os
import logging
import hashlib

import requests
import stripe
from flask import Flask, request, jsonify, render_template_string
from dotenv import load_dotenv

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv()

stripe.api_key             = os.environ["STRIPE_SECRET_KEY"]
STRIPE_WEBHOOK_SECRET      = os.getenv("STRIPE_WEBHOOK_SECRET", "")
TELEGRAM_BOT_TOKEN         = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID           = os.environ["TELEGRAM_CHAT_ID"]

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Mémorise les session_id déjà traités (en prod : utiliser Redis ou une DB)
_used_sessions: set[str] = set()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Page HTML ─────────────────────────────────────────────────────────────────

SUCCESS_PAGE = """
<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Accès confirmé</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0f0f0f;
      color: #f0f0f0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }
    .card {
      background: #1a1a1a;
      border: 1px solid #2a2a2a;
      border-radius: 16px;
      padding: 40px 32px;
      max-width: 440px;
      width: 100%;
      text-align: center;
    }
    .icon { font-size: 48px; margin-bottom: 16px; }
    h1 { font-size: 22px; font-weight: 600; margin-bottom: 8px; }
    p  { color: #888; font-size: 15px; line-height: 1.6; margin-bottom: 28px; }
    .btn {
      display: inline-block;
      background: #229ED9;
      color: #fff;
      text-decoration: none;
      padding: 14px 28px;
      border-radius: 10px;
      font-size: 16px;
      font-weight: 600;
      transition: background 0.2s;
    }
    .btn:hover { background: #1a8bbf; }
    .note { margin-top: 20px; font-size: 13px; color: #555; }

    /* État erreur */
    .error h1 { color: #e55; }
    .error .icon::after { content: "❌"; }
    .error .icon { font-size: 48px; }
  </style>
</head>
<body>
  <div class="card {% if error %}error{% endif %}">
    {% if error %}
      <div class="icon">❌</div>
      <h1>{{ error_title }}</h1>
      <p>{{ error_msg }}</p>
    {% else %}
      <div class="icon">✅</div>
      <h1>Paiement confirmé !</h1>
      <p>Merci {{ name }}. Clique sur le bouton pour rejoindre le canal Telegram.</p>
      <a class="btn" href="{{ invite_link }}" target="_blank">Rejoindre le canal Telegram</a>
      <p class="note">Ce lien est personnel et à usage unique.</p>
    {% endif %}
  </div>
</body>
</html>
"""

ERROR_PAGE = """
<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <title>Erreur</title>
  <style>
    body { font-family: sans-serif; background:#0f0f0f; color:#f0f0f0;
           display:flex; align-items:center; justify-content:center; min-height:100vh; }
    .card { background:#1a1a1a; border-radius:16px; padding:40px; max-width:400px; text-align:center; }
    h1 { color:#e55; }
    p  { color:#888; margin-top:12px; }
  </style>
</head>
<body>
  <div class="card">
    <h1>❌ {{ title }}</h1>
    <p>{{ message }}</p>
  </div>
</body>
</html>
"""

# ── Telegram ──────────────────────────────────────────────────────────────────

def create_invite_link(label: str = "") -> str:
    """Lien d'invitation Telegram à usage unique (1 seul membre)."""
    resp = requests.post(
        f"{TELEGRAM_API}/createChatInviteLink",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "member_limit": 1,
            "name": label[:32] if label else "Accès premium",
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram error: {data.get('description')}")
    return data["result"]["invite_link"]

# ── Route principale — redirection post-paiement ──────────────────────────────

@app.route("/success")
def success():
    session_id = request.args.get("session_id", "").strip()

    # Paramètre manquant
    if not session_id:
        return render_template_string(
            ERROR_PAGE,
            title="Lien invalide",
            message="Aucun identifiant de session fourni.",
        ), 400

    # Anti-rejeu : une session = un seul lien généré
    if session_id in _used_sessions:
        return render_template_string(
            ERROR_PAGE,
            title="Lien déjà utilisé",
            message="Ce lien d'accès a déjà été activé.",
        ), 403

    # Vérification du paiement auprès de Stripe
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except stripe.error.InvalidRequestError:
        return render_template_string(
            ERROR_PAGE,
            title="Session introuvable",
            message="Ce lien de paiement n'existe pas.",
        ), 404
    except stripe.error.StripeError as exc:
        log.error("Stripe API error: %s", exc)
        return render_template_string(
            ERROR_PAGE,
            title="Erreur Stripe",
            message="Impossible de vérifier le paiement. Réessaie dans quelques instants.",
        ), 502

    if session.payment_status != "paid":
        return render_template_string(
            ERROR_PAGE,
            title="Paiement non finalisé",
            message="Ton paiement n'a pas encore été validé.",
        ), 402

    # Génération du lien Telegram
    try:
        customer_name = (
            (session.customer_details and session.customer_details.name) or ""
        )
        invite_link = create_invite_link(label=customer_name or session_id[-8:])
    except Exception as exc:
        log.error("Telegram invite link error: %s", exc)
        return render_template_string(
            ERROR_PAGE,
            title="Erreur Telegram",
            message="Impossible de générer ton lien. Contacte le support.",
        ), 500

    # Marquer la session comme utilisée
    _used_sessions.add(session_id)
    log.info("Accès accordé — session=%s email=%s", session_id,
             session.customer_details and session.customer_details.email)

    return render_template_string(
        SUCCESS_PAGE,
        error=False,
        name=customer_name or "toi",
        invite_link=invite_link,
    )


# ── Webhook Stripe (secours) ──────────────────────────────────────────────────
# Utile si le client ferme la fenêtre avant la redirection.
# Dans ce cas, envoyer le lien par email nécessite de configurer SMTP (voir
# l'ancienne version du script).

@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({"status": "webhook disabled"}), 200

    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        return jsonify({"error": "Invalid signature"}), 400

    if event.type == "checkout.session.completed":
        obj = event.data.object
        if obj.payment_status == "paid":
            log.info("Webhook backup — session %s payée (lien non généré ici, "
                     "prévoir envoi email)", obj.id)
            # TODO : si tu veux le backup email, ajoute ici l'envoi SMTP
            # avec create_invite_link() + send_invite_email()

    return jsonify({"status": "ok"}), 200


# ── Sanity check ──────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


# ── Entrée ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)

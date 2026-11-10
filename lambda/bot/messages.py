"""User-facing enrollment and login messages, in English and Italian.

Language comes from Telegram's language_code on the sender; anything other
than Italian gets English.
"""

MESSAGES = {
    "en": {
        "requested": "👋 Access requested, {name}! I'll message you as soon as the admin decides.",
        "already_pending": "⏳ Your request is still pending. I'll let you know as soon as it's decided.",
        "reminded": "⏳ Still pending — I've reminded the admin.",
        "too_many": "🚧 Too many requests are waiting right now. Try again later.",
        "withdrawn": "🗑 Request withdrawn. Write to me again whenever you like.",
        "approved": "✅ You're in! Link your gym account with /login — I'll ask for email and password step by step.",
        "denied": "🚫 Your access request was not approved. Write to the admin if you think this is a mistake.",
        "not_authorized": "🚫 You are not authorized to use this bot.",
        "login_ask_email": "📧 What's your gym account email?",
        "login_bad_email": "That doesn't look like an email. Send it again, or /stop to abort.",
        "login_ask_password": "🔑 Now your password. I'll delete that message right away.",
        "login_aborted": "🛑 Login cancelled.",
        "login_failed": "❌ Login failed — the gym app rejected those credentials. Try /login again.",
        "login_linked": "✅ Linked gym account for {email} ({member} member, club {club}).",
    },
    "it": {
        "requested": "👋 Richiesta di accesso inviata, {name}! Ti scrivo appena l'admin decide.",
        "already_pending": "⏳ La tua richiesta è ancora in attesa. Ti avviso appena viene decisa.",
        "reminded": "⏳ Richiesta ancora in attesa — ho ricordato all'admin.",
        "too_many": "🚧 Ci sono troppe richieste in attesa in questo momento. Riprova più tardi.",
        "withdrawn": "🗑 Richiesta ritirata. Scrivimi di nuovo quando vuoi.",
        "approved": "✅ Sei dentro! Collega il tuo account della palestra con /login — ti chiedo email e password passo passo.",
        "denied": "🚫 La tua richiesta di accesso non è stata approvata. Scrivi all'admin se pensi sia un errore.",
        "not_authorized": "🚫 Non sei autorizzato a usare questo bot.",
        "login_ask_email": "📧 Qual è l'email del tuo account della palestra?",
        "login_bad_email": "Non sembra un'email. Riprova, oppure /stop per annullare.",
        "login_ask_password": "🔑 Ora la password. Cancello subito quel messaggio.",
        "login_aborted": "🛑 Login annullato.",
        "login_failed": "❌ Login fallito — la palestra ha rifiutato le credenziali. Riprova con /login.",
        "login_linked": "✅ Account della palestra collegato per {email} (membro {member}, club {club}).",
    },
}


def language_of(code: str | None) -> str:
    return "it" if (code or "").lower().startswith("it") else "en"


def t(key: str, lang: str = "en", **params) -> str:
    table = MESSAGES.get(lang, MESSAGES["en"])
    return table[key].format(**params)

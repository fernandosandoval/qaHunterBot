"""
telegram_notifier.py
Robust module to send alerts and attach PDF documents to Telegram via Bot API.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import ssl_setup  # noqa: F401  (trust the OS certificate store; must precede network imports)
import requests
from dotenv import load_dotenv

# Load variables from .env file
load_dotenv(override=True)


class TelegramNotifier:
    """Handles Telegram Bot API interactions for alert messages and PDF delivery."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> None:
        self.bot_token = (bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
        self.chat_id = (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"

    def is_configured(self) -> bool:
        """Checks whether necessary credentials are set and not placeholder values."""
        invalid_tokens = ["", "your_telegram_bot_token_here", "none"]
        invalid_chats = ["", "your_telegram_chat_id_here", "none"]
        return (
            bool(self.bot_token)
            and bool(self.chat_id)
            and self.bot_token.lower() not in invalid_tokens
            and self.chat_id.lower() not in invalid_chats
        )

    def send_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = True,
    ) -> bool:
        """
        Sends an HTML-formatted message to the designated chat.
        """
        if not self.is_configured():
            print(
                "[ERROR] Telegram credentials not configured. Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env",
                file=sys.stderr,
            )
            return False

        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }

        try:
            response = requests.post(url, json=payload, timeout=15)
            data = response.json()
            if response.status_code == 200 and data.get("ok"):
                return True

            print(
                f"[ERROR] Telegram sendMessage failed [HTTP {response.status_code}]: {data.get('description')}",
                file=sys.stderr,
            )
            return False
        except requests.RequestException as e:
            print(f"[ERROR] Network error contacting Telegram API: {e}", file=sys.stderr)
            return False

    def send_document(
        self,
        document_path: str | Path,
        caption: Optional[str] = None,
        parse_mode: str = "HTML",
    ) -> bool:
        """
        Uploads and sends a document (e.g. PDF CV) with an optional caption.
        """
        if not self.is_configured():
            print(
                "[ERROR] Telegram credentials not configured. Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env",
                file=sys.stderr,
            )
            return False

        path = Path(document_path)
        if not path.exists():
            print(f"[ERROR] Document file does not exist: {path}", file=sys.stderr)
            return False

        url = f"{self.base_url}/sendDocument"
        data = {
            "chat_id": self.chat_id,
            "parse_mode": parse_mode,
        }
        if caption:
            # Telegram document caption limit is 1024 chars
            data["caption"] = caption[:1024]

        try:
            with open(path, "rb") as file_handle:
                files = {"document": (path.name, file_handle, "application/pdf")}
                response = requests.post(url, data=data, files=files, timeout=30)

            result = response.json()
            if response.status_code == 200 and result.get("ok"):
                return True

            print(
                f"[ERROR] Telegram sendDocument failed [HTTP {response.status_code}]: {result.get('description')}",
                file=sys.stderr,
            )
            return False
        except requests.RequestException as e:
            print(f"[ERROR] Network error sending document to Telegram: {e}", file=sys.stderr)
            return False

    def send_notification(
        self,
        message_html: str,
        pdf_path: Optional[str | Path] = None,
    ) -> bool:
        """
        Convenience method:
        - Sends the detailed HTML message.
        - If pdf_path is supplied and valid, sends the PDF as well.
        """
        msg_ok = self.send_message(message_html)
        if not msg_ok:
            return False

        if pdf_path:
            pdf_caption = "📄 <b>CV Adaptado Adjunto (Down-leveled Mid QA)</b>"
            doc_ok = self.send_document(pdf_path, caption=pdf_caption)
            return doc_ok

        return True


def send_telegram_notification(
    text: str,
    pdf_path: Optional[str | Path] = None,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> bool:
    """Standalone helper function to send Telegram alert + optional PDF."""
    notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)
    return notifier.send_notification(message_html=text, pdf_path=pdf_path)


import html


def send_telegram_alert(
    match_reason: str = "",
    cover_letter: str = "",
    apply_url: str = "",
    pdf_path: Optional[str | Path] = None,
    job_title: str = "Freelance QA Opportunity",
    company: str = "Remote Client",
    rate_info: Optional[str] = None,
    workload_info: Optional[str] = None,
    score: Optional[int] = None,
    source: str = "",
    location: str = "",
    job_type: str = "",
    is_india: bool = False,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    **kwargs,
) -> bool:
    """
    Formats a rich HTML alert with match reason, cover letter/proposal, and direct link,
    and sends it alongside the tailored PDF.
    """
    clean_title = html.escape(str(job_title).strip())
    clean_company = html.escape(str(company).strip())
    clean_reason = html.escape(str(match_reason).strip())
    clean_cl = html.escape(str(cover_letter).strip())
    clean_url = html.escape(str(apply_url).strip())
    clean_source = html.escape(str(source).strip())
    clean_location = html.escape(str(location).strip())
    clean_job_type = html.escape(str(job_type).strip())

    region_badge = "🇮🇳 India / APAC" if is_india else "🌍 Remoto Global"
    if clean_location and "india" in clean_location.lower():
        region_badge = "🇮🇳 India / APAC"

    lines = [
        "🎯 <b>¡OPORTUNIDAD QA VIABLE DETECTADA!</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"💼 <b>Puesto:</b> {clean_title}",
        f"🏢 <b>Cliente:</b> {clean_company}",
    ]

    if clean_source:
        lines.append(f"🌐 <b>Plataforma:</b> {clean_source}")
    if clean_job_type:
        lines.append(f"🏷 <b>Modalidad:</b> {clean_job_type}")
    if clean_location:
        lines.append(f"📍 <b>Mercado / Región:</b> {clean_location} ({region_badge})")
    if rate_info:
        lines.append(f"💰 <b>Tarifa / Presupuesto:</b> {html.escape(str(rate_info).strip())}")
    if workload_info:
        lines.append(f"⏱ <b>Carga horaria:</b> {html.escape(str(workload_info).strip())}")
    if score is not None:
        lines.append(f"📊 <b>Fit Score:</b> {score}/100")

    is_fixed_or_freelance = any(k in clean_job_type.lower() or k in clean_source.lower() for k in ["freelance", "fixed", "proyecto", "gig"])
    proposal_label = "📝 <b>Propuesta / Bid Sugerido (Freelance):</b>" if is_fixed_or_freelance else "📝 <b>Cover Letter sugerido:</b>"

    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━",
        f"💡 <b>Match Reason:</b>\n{clean_reason}",
        "",
        proposal_label,
        f"<blockquote>{clean_cl}</blockquote>",
        "",
        f'🚀 <b>Postular con 1 clic:</b>\n<a href="{clean_url}">👉 Abrir oferta y postular</a>',
    ])

    formatted_text = "\n".join(lines)
    return send_telegram_notification(
        text=formatted_text,
        pdf_path=pdf_path,
        bot_token=bot_token,
        chat_id=chat_id,
    )



if __name__ == "__main__":
    print("=" * 60)
    print(">> Telegram Notifier Connection Test")
    print("=" * 60)

    notifier = TelegramNotifier()
    if not notifier.is_configured():
        print("\n[!] AVISO: Las credenciales de Telegram aun no estan configuradas en .env")
        print("    Para obtener tus credenciales:")
        print("    1. Crea un bot en Telegram con @BotFather y copia el HTTP API TOKEN.")
        print("    2. Abre un chat con tu nuevo bot y enviale un mensaje (ej: /start).")
        print("    3. Obten tu Chat ID personal hablando con @userinfobot o @GetIDsBot.")
        print("    4. Pega ambos valores en tu archivo .env:")
        print("       TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRstuVWXyz")
        print("       TELEGRAM_CHAT_ID=987654321\n")
        sys.exit(0)

    print(f"[*] Verificando envio a Chat ID: {notifier.chat_id}...")
    test_message = (
        "🤖 <b>qa-freelance-agent: Test de Conexion Exitoso</b>\n\n"
        "Tu bot de Telegram esta correctamente vinculado y listo para recibir alertas "
        "de oportunidades freelance con CVs generados."
    )

    # Check if a sample PDF exists to test document delivery as well
    sample_pdf = Path("output/sample_cv.pdf")
    pdf_to_send = sample_pdf if sample_pdf.exists() else None

    success = notifier.send_notification(test_message, pdf_path=pdf_to_send)
    if success:
        print("[SUCCESS] Notificacion enviada exitosamente a tu Telegram!")
        if pdf_to_send:
            print(f"[SUCCESS] Documento PDF ({pdf_to_send.name}) adjuntado correctamente.")
    else:
        print("[FAILURE] No se pudo enviar el mensaje. Revisa los logs de error arriba.")

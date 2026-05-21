"""PDF generation utilities.

Currently used for the guest-portal rental-agreement: render text + the
guest's signature PNG into a single-page PDF that gets attached to the
Cloudbeds reservation. Kept dep-light (reportlab only) so we don't drag
in a headless browser to do something a 30-line module handles.
"""
import base64
import io
import logging
from datetime import datetime

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image as RLImage, KeepTogether,
)

log = logging.getLogger(__name__)


def _decode_signature_png(signature_data_url: str) -> bytes | None:
    """Strip the data URL prefix off a canvas.toDataURL() result and return
    raw PNG bytes. Returns None when the input doesn't look like a PNG."""
    if not signature_data_url:
        return None
    # Expected format: "data:image/png;base64,iVBORw0KGgo..."
    if "," in signature_data_url:
        prefix, _, b64 = signature_data_url.partition(",")
        if "image/png" not in prefix.lower():
            return None
        try:
            return base64.b64decode(b64)
        except Exception:
            return None
    # Bare base64 (no data URL) -- accept it but only if it decodes.
    try:
        return base64.b64decode(signature_data_url)
    except Exception:
        return None


def signature_png_looks_drawn(png_bytes: bytes | None, min_bytes: int = 800) -> bool:
    """Heuristic: an empty 'cleared' canvas PNG is small (just headers +
    transparent/white pixels compressed). A real drawn signature has
    enough pixel data to push the PNG well past min_bytes. 800 is a
    conservative threshold -- a single-stroke signature is typically
    3-15 KB. Defends against accidental form submits without a drawing."""
    if not png_bytes:
        return False
    return len(png_bytes) >= min_bytes


def render_agreement_pdf(
    *,
    hotel_name: str,
    hotel_address: str,
    hotel_phone: str,
    guest_name: str,
    reservation_id: str,
    check_in: str,
    check_out: str,
    agreement_text: str,
    agreement_version: str,
    typed_name: str,
    signature_png: bytes,
    signed_at_utc: datetime,
) -> bytes:
    """Render the signed agreement to PDF bytes. The signature image
    is embedded inline at signature size.

    Layout (single page, letter):
      Title:  "<hotel> -- Rental Agreement"
      Reservation header (4-line block: guest, reservation #, dates, phone)
      Agreement body (long paragraph, preserves newlines as paragraph breaks)
      Signature row: image (200x60px-ish) above typed name + signed-at line
      Footer: agreement version, "Generated <timestamp>" for forensic match.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        title=f"{hotel_name} -- Rental Agreement",
        author=hotel_name,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "title", parent=styles["Title"], alignment=TA_LEFT, fontSize=18,
        spaceAfter=14,
    )
    meta_style = ParagraphStyle("meta", parent=styles["Normal"], fontSize=10, leading=14, textColor="#475569")
    body_style = ParagraphStyle("body", parent=styles["BodyText"], fontSize=10.5, leading=15)
    footer_style = ParagraphStyle("footer", parent=styles["Normal"], fontSize=8, textColor="#94a3b8")

    story: list = []
    story.append(Paragraph(f"{hotel_name} &mdash; Rental Agreement", title_style))
    meta_lines = [
        f"<b>Guest:</b> {guest_name or '&mdash;'}",
        f"<b>Reservation:</b> {reservation_id or '&mdash;'}",
        f"<b>Stay:</b> {check_in or '?'} &nbsp;&rarr;&nbsp; {check_out or '?'}",
    ]
    if hotel_address:
        meta_lines.append(f"{hotel_address}")
    if hotel_phone:
        meta_lines.append(hotel_phone)
    for line in meta_lines:
        story.append(Paragraph(line, meta_style))
    story.append(Spacer(1, 14))

    # Agreement body -- split on blank lines into paragraphs.
    body_blocks = [p.strip() for p in (agreement_text or "").split("\n\n") if p.strip()]
    if not body_blocks:
        body_blocks = ["(no agreement text on file)"]
    for block in body_blocks:
        # Convert single newlines to soft line breaks
        block_html = block.replace("\n", "<br/>")
        story.append(Paragraph(block_html, body_style))
        story.append(Spacer(1, 6))

    story.append(Spacer(1, 16))

    # Signature row -- the image and the typed/date block are kept together
    # so they don't split across pages.
    sig_buf = io.BytesIO(signature_png)
    sig_img = RLImage(sig_buf, width=2.5 * inch, height=0.8 * inch)
    sig_img.hAlign = "LEFT"
    signed_local = signed_at_utc.astimezone()  # local time of the server; good enough for the audit
    signature_block: list = [
        sig_img,
        Spacer(1, 4),
        Paragraph(f"<b>Signed by:</b> {typed_name or guest_name or '(no name typed)'}", meta_style),
        Paragraph(f"<b>Signed at:</b> {signed_local.strftime('%Y-%m-%d %H:%M %Z')}", meta_style),
    ]
    story.append(KeepTogether(signature_block))

    story.append(Spacer(1, 20))
    story.append(Paragraph(
        f"Agreement version {agreement_version} &middot; Generated "
        f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        footer_style,
    ))

    doc.build(story)
    pdf_bytes = buf.getvalue()
    buf.close()
    log.info(
        "render_agreement_pdf: res=%s name=%s bytes=%d",
        reservation_id, guest_name or "?", len(pdf_bytes),
    )
    return pdf_bytes

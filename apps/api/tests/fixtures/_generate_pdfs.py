"""Generate sample_text.pdf and sample_with_image.pdf fixtures.

Run once: ``python tests/fixtures/_generate_pdfs.py``. The fixtures are
committed alongside the test that uses them.
"""
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch


def make_text_pdf(path: str) -> None:
    """3-page PDF with text-only content (password reset topic)."""
    doc = SimpleDocTemplate(path, pagesize=letter)
    styles = getSampleStyleSheet()
    story = []
    story.append(Paragraph("Password Reset Guide", styles["Heading1"]))
    story.append(Paragraph(
        "To reset your password, navigate to the Settings page. "
        "Click on Account, then click the Reset Password button. "
        "You will receive a reset link via email.",
        styles["BodyText"],
    ))
    story.append(PageBreak())
    story.append(Paragraph("Page 2: Troubleshooting", styles["Heading2"]))
    story.append(Paragraph(
        "If you cannot log in, try resetting your password. The reset email "
        "may take up to 5 minutes. Check your spam if not received.",
        styles["BodyText"],
    ))
    story.append(PageBreak())
    story.append(Paragraph("Page 3: FAQ", styles["Heading2"]))
    story.append(Paragraph(
        "Q: I never received the reset email. A: Check spam folder. "
        "Q: Reset link expired. A: Request a new reset link from the login page.",
        styles["BodyText"],
    ))
    doc.build(story)


def make_image_pdf(path: str) -> None:
    """2-page PDF; page 2 has a table (key-page heuristic trigger)."""
    doc = SimpleDocTemplate(path, pagesize=letter)
    styles = getSampleStyleSheet()
    story = []
    story.append(Paragraph("Billing FAQ", styles["Heading1"]))
    story.append(Paragraph("Page 1 is text-only.", styles["BodyText"]))
    story.append(PageBreak())
    story.append(Paragraph("Page 2 — Pricing table:", styles["Heading2"]))
    table = Table([
        ["Plan", "Price", "Users"],
        ["Free", "$0", "1"],
        ["Pro", "$10", "5"],
        ["Team", "$50", "20"],
    ])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), "#cccccc"),
        ("GRID", (0, 0), (-1, -1), 1, "#000000"),
    ]))
    story.append(table)
    doc.build(story)


if __name__ == "__main__":
    import os
    base = os.path.dirname(os.path.abspath(__file__))
    make_text_pdf(os.path.join(base, "sample_text.pdf"))
    make_image_pdf(os.path.join(base, "sample_with_image.pdf"))
    print(f"Generated fixtures in {base}")

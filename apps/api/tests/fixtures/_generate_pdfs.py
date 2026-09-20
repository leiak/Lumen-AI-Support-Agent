"""Generate sample PDF fixtures for the PDF processor tests.

Run once: ``python tests/fixtures/_generate_pdfs.py``. The fixtures are
committed alongside the test that uses them.

Local poppler note: image-PDF screenshot tests require poppler on PATH
(Dockerfile installs poppler-utils for production; local Windows dev
needs to extract poppler-24.x.x binaries and add bin/ to PATH).
"""
import os

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Table,
    TableStyle,
)


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
    """2-page PDF; page 2 has an embedded image (covers image branch)."""
    doc = SimpleDocTemplate(path, pagesize=letter)
    styles = getSampleStyleSheet()
    story = []
    story.append(Paragraph("Image Demo", styles["Heading1"]))
    story.append(Paragraph("Page 1 is text-only.", styles["BodyText"]))
    story.append(PageBreak())
    story.append(Paragraph("Page 2 — embedded image:", styles["Heading2"]))
    # Generate a tiny PNG inline for the image
    img_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_sample_image.png"
    )
    if not os.path.exists(img_path):
        from PIL import Image as PILImage

        img = PILImage.new("RGB", (100, 100), color="red")
        img.save(img_path, "PNG")
    story.append(Image(img_path, width=2 * inch, height=2 * inch))
    doc.build(story)


def make_table_pdf(path: str) -> None:
    """2-page PDF; page 2 has a table (covers table branch)."""
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
    base = os.path.dirname(os.path.abspath(__file__))
    make_text_pdf(os.path.join(base, "sample_text.pdf"))
    make_table_pdf(os.path.join(base, "sample_with_table.pdf"))
    make_image_pdf(os.path.join(base, "sample_with_image.pdf"))
    print(f"Generated fixtures in {base}")

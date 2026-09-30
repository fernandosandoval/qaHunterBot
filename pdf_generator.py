"""
pdf_generator.py
Converts Markdown CV content into an elegant, professional PDF document using ReportLab.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)


def _markdown_to_reportlab_xml(text: str) -> str:
    """
    Translates common Markdown inline formatting into safe ReportLab XML tags:
    Escapes &, <, > safely first, then converts:
    **bold** -> <b>bold</b>
    *italic* -> <i>italic</i>
    `code` -> <font name="Courier">code</font>
    [link](url) -> <a href="url"><u>link</u></a>
    """
    # Escape ampersands not part of entities
    text = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;)", "&amp;", text)
    # Escape raw angle brackets
    text = text.replace("<", "&lt;").replace(">", "&gt;")

    # Bold: **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)

    # Italic: *text* or _text_
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<i>\1</i>", text)

    # Code inline: `text`
    text = re.sub(r"`(.+?)`", r'<font name="Courier" color="#2B6CB0">\1</font>', text)

    # Links: [text](url)
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2" color="#2B6CB0"><u>\1</u></a>', text)

    return text.strip()


def generate_pdf_from_markdown(markdown_content: str, output_path: str | Path) -> str:
    """
    Parses a Markdown CV string and renders a modern, high-quality PDF.
    
    Args:
        markdown_content: The CV in Markdown format.
        output_path: Destination file path for the PDF (e.g. output/cv.pdf).
        
    Returns:
        Absolute string path to the generated PDF.
    """
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        leftMargin=36,  # 0.5 in
        rightMargin=36,
        topMargin=36,
        bottomMargin=36,
    )
    
    styles = getSampleStyleSheet()
    
    # Professional Color Palette
    PRIMARY_COLOR = colors.HexColor("#1A365D")    # Dark Navy
    SECONDARY_COLOR = colors.HexColor("#2B6CB0")  # Royal/Slate Blue
    TEXT_COLOR = colors.HexColor("#2D3748")       # Charcoal
    MUTED_COLOR = colors.HexColor("#4A5568")      # Subdued Charcoal
    LINE_COLOR = colors.HexColor("#CBD5E0")       # Divider line
    
    title_style = ParagraphStyle(
        "CVTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        textColor=PRIMARY_COLOR,
        alignment=0,  # Left
        spaceAfter=4,
    )
    
    contact_style = ParagraphStyle(
        "CVContact",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=13,
        textColor=MUTED_COLOR,
        spaceAfter=10,
    )
    
    h2_style = ParagraphStyle(
        "CVH2",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=15,
        textColor=PRIMARY_COLOR,
        spaceBefore=10,
        spaceAfter=3,
        keepWithNext=True,
    )
    
    h3_style = ParagraphStyle(
        "CVH3",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=13,
        textColor=SECONDARY_COLOR,
        spaceBefore=6,
        spaceAfter=2,
        keepWithNext=True,
    )
    
    body_style = ParagraphStyle(
        "CVBody",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=13,
        textColor=TEXT_COLOR,
        spaceAfter=4,
    )
    
    bullet_style = ParagraphStyle(
        "CVBullet",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=12.5,
        textColor=TEXT_COLOR,
        leftIndent=12,
        firstLineIndent=-8,
        spaceAfter=2,
    )
    
    story = []
    lines = markdown_content.strip().split("\n")
    is_first_header = True
    
    for raw_line in lines:
        line = raw_line.strip()
        
        if not line:
            continue
            
        # Horizontal Rule (---)
        if re.match(r"^---+$", line):
            story.append(Spacer(1, 2))
            story.append(HRFlowable(width="100%", thickness=0.75, color=LINE_COLOR, spaceAfter=6, spaceBefore=4))
            continue
            
        # Header 1 (# ...)
        if line.startswith("# "):
            text = _markdown_to_reportlab_xml(line[2:])
            if is_first_header:
                story.append(Paragraph(text, title_style))
                is_first_header = False
            else:
                story.append(Paragraph(text, h2_style))
            continue
            
        # Header 2 (## ...)
        if line.startswith("## "):
            text = _markdown_to_reportlab_xml(line[3:])
            story.append(Spacer(1, 4))
            story.append(Paragraph(text.upper(), h2_style))
            story.append(HRFlowable(width="100%", thickness=0.5, color=SECONDARY_COLOR, spaceAfter=4, spaceBefore=1))
            continue
            
        # Header 3 / 4 (### ... or #### ...) -> role / sub-section style
        if line.startswith("#### ") or line.startswith("### "):
            text = _markdown_to_reportlab_xml(line.lstrip("#").strip())
            story.append(Paragraph(text, h3_style))
            continue
            
        # Bullet list item (- ... or * ...)
        if re.match(r"^[-*]\s+", line):
            bullet_text = re.sub(r"^[-*]\s+", "", line)
            xml_text = f"&bull; {_markdown_to_reportlab_xml(bullet_text)}"
            story.append(Paragraph(xml_text, bullet_style))
            continue
            
        # Contact line (contains contact or email or starts with **Contact)
        if "Contact:" in line or "@" in line and len(story) < 3:
            xml_text = _markdown_to_reportlab_xml(line)
            story.append(Paragraph(xml_text, contact_style))
            continue
            
        # Regular paragraph
        xml_text = _markdown_to_reportlab_xml(line)
        story.append(Paragraph(xml_text, body_style))
        
    doc.build(story)
    return str(output_path)


# Friendly alias for orchestrators
convert_markdown_to_pdf = generate_pdf_from_markdown


if __name__ == "__main__":
    sample_md = """# Alex Morgan - QA Automation & Manual Tester
**Contact:** alex.qa@freelance.dev | Location: Remote | LinkedIn: linkedin.com/in/alex-qa-dev

---

## Professional Summary
Mid-level Quality Assurance Engineer with hands-on experience in manual exploratory testing, API verification with Postman, and automated regression scripts using Python and Playwright. Focused on pragmatic, reliable bug discovery and fast turnaround times for agile teams.

## Core Skills
- **Manual QA:** Exploratory testing, test case design, cross-browser verification, bug triage in Jira.
- **Automation:** Python, Pytest, Playwright, Selenium.
- **API Testing:** Postman, curl, RESTful JSON APIs.
- **Agile & Tools:** Git, GitHub Actions, Jira, Trello, Chrome DevTools.

## Experience
### QA Engineer | Freelance & Contract
*2022 - Present*
- Executed end-to-end exploratory and functional testing on web and mobile web apps.
- Created and maintained automated regression test suites reducing manual testing overhead by 50%.
- Documented clear, reproducible bug reports with reproduction steps and network traces.

### QA Tester | DevCraft Studio
*2020 - 2022*
- Performed regression, smoke, and sanity testing for bi-weekly sprint releases.
- Validated REST APIs and backend responses using Postman test collections.
"""
    test_pdf_path = Path("output/sample_cv.pdf")
    generated = generate_pdf_from_markdown(sample_md, test_pdf_path)
    print(f"[OK] PDF generated successfully at: {generated}")

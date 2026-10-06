"""
pdf_generator.py
Converts Markdown CV content into a clean, ATS-friendly single-column PDF using ReportLab.

Robustness features:
- Unicode TTF fonts (Arial / DejaVu / bundled Vera) with automatic fallback to Helvetica.
- Text sanitising: smart quotes, arrows, emojis and glyphs missing from the font never
  render as black boxes.
- Per-paragraph safety net: a malformed inline tag only degrades that one line to plain text.
- Role blocks are kept together across page breaks.
- Last-resort plain-text PDF if the rich renderer fails for any reason.
"""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Optional

import reportlab
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer

# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

_BUNDLED_FONTS = Path(reportlab.__file__).parent / "fonts"

# (family name, [regular, bold, italic, bold-italic] file names, search directories)
_FONT_CANDIDATES: list[tuple[str, list[str], list[Path]]] = [
    (
        "CVArial",
        ["arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"],
        [
            Path(r"C:\Windows\Fonts"),
            Path("/usr/share/fonts/truetype/msttcorefonts"),
            Path("/Library/Fonts"),
            Path("/System/Library/Fonts/Supplemental"),
        ],
    ),
    (
        "CVDejaVu",
        ["DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans-Oblique.ttf", "DejaVuSans-BoldOblique.ttf"],
        [Path("/usr/share/fonts/truetype/dejavu"), Path("/usr/share/fonts/dejavu")],
    ),
    (
        "CVVera",
        ["Vera.ttf", "VeraBd.ttf", "VeraIt.ttf", "VeraBI.ttf"],
        [_BUNDLED_FONTS],
    ),
]

_font_cache: Optional[dict[str, str]] = None


def _register_fonts() -> dict[str, str]:
    """
    Registers the first available TTF family and returns
    {"regular", "bold", "italic", "bolditalic", "mono", "unicode"} font names.
    Falls back to the built-in Helvetica family (cp1252 only) if nothing loads.
    PDF_FONT_DIR (env) may point to a directory with a custom family to try first.
    """
    global _font_cache
    if _font_cache is not None:
        return _font_cache

    candidates = list(_FONT_CANDIDATES)
    custom_dir = os.getenv("PDF_FONT_DIR", "").strip()
    if custom_dir:
        candidates = [("CVCustom", ["regular.ttf", "bold.ttf", "italic.ttf", "bolditalic.ttf"], [Path(custom_dir)])] + candidates

    for family, files, dirs in candidates:
        for directory in dirs:
            paths = [directory / f for f in files]
            if not all(p.exists() for p in paths):
                continue
            try:
                names = [f"{family}-{suffix}" for suffix in ("Regular", "Bold", "Italic", "BoldItalic")]
                for name, path in zip(names, paths):
                    if name not in pdfmetrics.getRegisteredFontNames():
                        pdfmetrics.registerFont(TTFont(name, str(path)))
                pdfmetrics.registerFontFamily(
                    family, normal=names[0], bold=names[1], italic=names[2], boldItalic=names[3]
                )
                _font_cache = {
                    "regular": names[0],
                    "bold": names[1],
                    "italic": names[2],
                    "bolditalic": names[3],
                    "mono": "Courier",
                    "unicode": "1",
                }
                return _font_cache
            except Exception as exc:  # corrupt font, permissions, etc. -> try the next family
                print(f"[WARN] No se pudo cargar la fuente {family} desde {directory}: {exc}", file=sys.stderr)

    print("[WARN] Sin fuentes TTF disponibles; usando Helvetica (solo caracteres cp1252).", file=sys.stderr)
    _font_cache = {
        "regular": "Helvetica",
        "bold": "Helvetica-Bold",
        "italic": "Helvetica-Oblique",
        "bolditalic": "Helvetica-BoldOblique",
        "mono": "Courier",
        "unicode": "",
    }
    return _font_cache


# ---------------------------------------------------------------------------
# Text sanitising
# ---------------------------------------------------------------------------

_TRANSLIT = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201c": '"', "\u201d": '"', "\u201e": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u2022": "-", "\u00b7": "|", "\u25cf": "-",
    "\u2192": "->", "\u2190": "<-", "\u21d2": "=>", "\u2713": "-", "\u2714": "-", "\u2705": "-",
    "\u2026": "...", "\u00a0": " ", "\u200b": "", "\u200c": "", "\u200d": "", "\ufeff": "",
    "\u2009": " ", "\u202f": " ", "\u2002": " ", "\u2003": " ",
}


def _font_supports(font_name: str, char: str) -> bool:
    try:
        face = pdfmetrics.getFont(font_name).face
        return ord(char) in face.charToGlyph
    except Exception:
        return True  # unknown -> let the renderer try


def sanitize_text(text: str, fonts: Optional[dict[str, str]] = None) -> str:
    """Normalises problematic characters so that no glyph renders as a black box."""
    fonts = fonts or _register_fonts()
    out: list[str] = []
    for ch in text:
        ch = _TRANSLIT.get(ch, ch)
        for c in ch:
            if c in ("\n", "\t"):
                out.append(c)
                continue
            cat = unicodedata.category(c)
            if cat in ("Cc", "Cf", "Cs", "Co", "Cn") or ord(c) > 0xFFFF or cat == "So":
                continue  # control chars, emojis and pictographs
            if fonts["unicode"]:
                if _font_supports(fonts["regular"], c):
                    out.append(c)
                else:
                    decomposed = unicodedata.normalize("NFKD", c).encode("ascii", "ignore").decode()
                    out.append(decomposed)
            else:
                out.append(c.encode("cp1252", "ignore").decode("cp1252"))
    return "".join(out)


# ---------------------------------------------------------------------------
# Inline markdown -> ReportLab XML
# ---------------------------------------------------------------------------

_PH_OPEN, _PH_CLOSE = "\ue000", "\ue001"
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BARE_RE = re.compile(r"(https?://[^\s<>)\]]+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+)")


def _escape_xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _markdown_to_reportlab_xml(text: str) -> str:
    """
    Translates inline Markdown into safe ReportLab XML.
    Links, bare URLs, e-mails and code spans are protected by placeholders first, so
    underscores or asterisks inside them can never be interpreted as formatting.
    """
    protected: list[str] = []

    def stash(xml: str) -> str:
        protected.append(xml)
        return f"{_PH_OPEN}{len(protected) - 1}{_PH_CLOSE}"

    def link_sub(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        safe_url = _escape_xml(url).replace('"', "%22")
        return stash(f'<a href="{safe_url}" color="#2B6CB0"><u>{_escape_xml(label)}</u></a>')

    text = _LINK_RE.sub(link_sub, text)
    text = re.sub(r"`([^`]+)`", lambda m: stash(f'<font name="Courier" color="#2B6CB0">{_escape_xml(m.group(1))}</font>'), text)
    text = _BARE_RE.sub(lambda m: stash(_escape_xml(m.group(1))), text)

    text = _escape_xml(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"<b>\1</b>", text)
    text = re.sub(r"\*(?!\s)(.+?)(?<!\s)\*", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"<i>\1</i>", text)

    text = re.sub(f"{_PH_OPEN}(\\d+){_PH_CLOSE}", lambda m: protected[int(m.group(1))], text)
    return text.strip()


def _plain(text: str) -> str:
    """Strips Markdown markers and returns escaped plain text (last-resort for a single line)."""
    text = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    text = re.sub(r"[*_`]+", "", text)
    return _escape_xml(text).strip()


def _safe_paragraph(raw_text: str, style: ParagraphStyle, prefix: str = "", upper: bool = False) -> Paragraph:
    """Builds a Paragraph; if inline tags are malformed, degrades to plain text for this line only."""
    source = raw_text.upper() if upper else raw_text
    try:
        return Paragraph(prefix + _markdown_to_reportlab_xml(source), style)
    except Exception:
        return Paragraph(prefix + _plain(source), style)


# ---------------------------------------------------------------------------
# Rich renderer
# ---------------------------------------------------------------------------

def _build_styles(fonts: dict[str, str]) -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()["Normal"]
    primary = colors.HexColor("#1A365D")
    secondary = colors.HexColor("#2B6CB0")
    text_color = colors.HexColor("#2D3748")
    muted = colors.HexColor("#4A5568")

    def style(name: str, **kw) -> ParagraphStyle:
        return ParagraphStyle(name, parent=base, **kw)

    return {
        "title": style("CVTitle", fontName=fonts["bold"], fontSize=18, leading=22, textColor=primary, spaceAfter=4),
        "contact": style("CVContact", fontName=fonts["regular"], fontSize=9, leading=13, textColor=muted, spaceAfter=2),
        "h2": style("CVH2", fontName=fonts["bold"], fontSize=11, leading=15, textColor=primary,
                    spaceBefore=10, spaceAfter=3, keepWithNext=True),
        "h3": style("CVH3", fontName=fonts["bold"], fontSize=10, leading=13, textColor=secondary,
                    spaceBefore=6, spaceAfter=2, keepWithNext=True),
        "body": style("CVBody", fontName=fonts["regular"], fontSize=9, leading=13, textColor=text_color, spaceAfter=4),
        "bullet": style("CVBullet", fontName=fonts["regular"], fontSize=9, leading=12.5, textColor=text_color,
                        leftIndent=14, firstLineIndent=-9, spaceAfter=2),
        "bullet2": style("CVBullet2", fontName=fonts["regular"], fontSize=9, leading=12.5, textColor=text_color,
                         leftIndent=26, firstLineIndent=-9, spaceAfter=2),
    }


def _markdown_to_story(markdown_content: str, fonts: dict[str, str]) -> list:
    st = _build_styles(fonts)
    line_color = colors.HexColor("#CBD5E0")
    secondary = colors.HexColor("#2B6CB0")

    story: list = []
    seen_title = False
    in_header_block = False  # contact lines between the title and the first section/rule

    for raw_line in markdown_content.strip().split("\n"):
        stripped = raw_line.strip()
        if not stripped:
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" \t"))

        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            in_header_block = False
            story.append(HRFlowable(width="100%", thickness=0.75, color=line_color, spaceAfter=6, spaceBefore=4))
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            level, title = len(heading.group(1)), heading.group(2).strip()
            if level == 1 and not seen_title:
                story.append(_safe_paragraph(title, st["title"]))
                seen_title = True
                in_header_block = True
            elif level <= 2:
                in_header_block = False
                story.append(_safe_paragraph(title, st["h2"], upper=True))
                story.append(HRFlowable(width="100%", thickness=0.5, color=secondary, spaceAfter=4, spaceBefore=1))
            else:
                in_header_block = False
                story.append(_safe_paragraph(title, st["h3"]))
            continue

        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        if bullet:
            in_header_block = False
            style = st["bullet2"] if indent >= 2 else st["bullet"]
            story.append(_safe_paragraph(bullet.group(1), style, prefix="&bull; "))
            continue

        if in_header_block:
            story.append(_safe_paragraph(stripped, st["contact"]))
            continue

        story.append(_safe_paragraph(stripped, st["body"]))

    return _keep_roles_together(story, st["h3"])


def _keep_roles_together(story: list, h3_style: ParagraphStyle) -> list:
    """Wraps each role heading + the next two flowables (dates, first bullet) so they never split."""
    result: list = []
    i = 0
    while i < len(story):
        item = story[i]
        if isinstance(item, Paragraph) and item.style.name == h3_style.name:
            group = story[i : i + 3]
            result.append(KeepTogether(group))
            i += len(group)
        else:
            result.append(item)
            i += 1
    return result


def _generate_rich_pdf(markdown_content: str, output_path: Path) -> None:
    fonts = _register_fonts()
    clean = sanitize_text(markdown_content, fonts)
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        leftMargin=40,
        rightMargin=40,
        topMargin=36,
        bottomMargin=36,
        title="Curriculum Vitae",
    )
    doc.build(_markdown_to_story(clean, fonts))


# ---------------------------------------------------------------------------
# Plain-text last resort
# ---------------------------------------------------------------------------

def _generate_plain_pdf(markdown_content: str, output_path: Path) -> None:
    """Minimal PDF using only built-in Helvetica (cp1252). Cannot fail on tags or fonts."""
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import simpleSplit

    text = markdown_content.replace("\r", "")
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_`]+", "", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"\1 (\2)", text)
    for src, dst in _TRANSLIT.items():
        text = text.replace(src, dst)
    text = text.encode("cp1252", "ignore").decode("cp1252")

    width, height = letter
    margin, font, size, leading = 48, "Helvetica", 10, 13
    c = canvas.Canvas(str(output_path), pagesize=letter)
    y = height - margin
    for paragraph in text.split("\n"):
        lines = simpleSplit(paragraph, font, size, width - 2 * margin) or [""]
        for line in lines:
            if y < margin:
                c.showPage()
                y = height - margin
            c.setFont(font, size)
            c.drawString(margin, y, line)
            y -= leading
    c.save()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_pdf_from_markdown(markdown_content: str, output_path: str | Path) -> str:
    """
    Renders a Markdown CV to PDF. Tries the rich renderer first and falls back to a plain-text
    PDF so that the pipeline always has a CV to attach.

    Returns the absolute path of the generated PDF.
    Raises ValueError for empty content, RuntimeError if even the fallback fails.
    """
    if not markdown_content or not markdown_content.strip():
        raise ValueError("El contenido Markdown del CV esta vacio.")

    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        _generate_rich_pdf(markdown_content, output_path)
        return str(output_path)
    except Exception as exc:
        print(f"[WARN] Renderizado rico del PDF fallo ({exc}); generando PDF de texto plano.", file=sys.stderr)

    try:
        _generate_plain_pdf(markdown_content, output_path)
        return str(output_path)
    except Exception as exc:
        raise RuntimeError(f"No se pudo generar el PDF ni con el fallback: {exc}") from exc


# Friendly alias for orchestrators
convert_markdown_to_pdf = generate_pdf_from_markdown


if __name__ == "__main__":
    sample_md = """# Alex Morgan - QA Automation Engineer
alex.qa@freelance.dev | Remote | linkedin.com/in/alex_qa_dev
[Portfolio](https://example.com/my_portfolio?a=1&b=2)

---

## Professional Summary
QA Automation Engineer with hands-on experience in API verification with Postman and regression suites using Python and Playwright. Reduced defects 20% → faster releases ✓

## Core Skills
- **Automation:** Python, Pytest, Playwright, Selenium.
- **API Testing:** Postman, REST APIs.
  - Contract checks with `curl`

## Professional Experience
### QA Engineer - Health Note Inc.
*Aug 2019 - Dec 2023 | Remote (California, USA)*
- Built end-to-end suites with Playwright and Cypress.
- Unclosed **bold marker and a <tag> that must not break the PDF.
"""
    generated = generate_pdf_from_markdown(sample_md, Path("output/sample_cv.pdf"))
    print(f"[OK] PDF generated successfully at: {generated}")

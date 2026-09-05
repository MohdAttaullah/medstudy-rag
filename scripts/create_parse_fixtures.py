"""Generate synthetic parsing fixtures.

Every fixture is original synthetic content written for this repository. No copyrighted medical
textbook, question bank or guideline text is used, and none of the wording is intended to be
medically meaningful: these documents test structural fidelity (pages, headings, reading order,
tables, figures, formulas, headers/footers, OCR fallback), never clinical correctness.

Run with:  uv run python scripts/create_parse_fixtures.py
"""

from pathlib import Path

from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.pdfgen.canvas import Canvas

ROOT = Path(__file__).resolve().parents[1] / "backend/tests/fixtures/parsing"
WIDTH, HEIGHT = LETTER


def _new(path: Path) -> Canvas:
    ROOT.mkdir(parents=True, exist_ok=True)
    surface = canvas.Canvas(str(path), pagesize=LETTER)
    surface.setTitle(path.stem)
    surface.setAuthor("Medical RAG synthetic parsing fixtures")
    return surface


def _heading(surface: Canvas, y: float, text: str, size: int = 14) -> float:
    surface.setFont("Helvetica-Bold", size)
    surface.drawString(inch, y, text)
    return y - 24


def _body(surface: Canvas, y: float, lines: list[str]) -> float:
    surface.setFont("Helvetica", 11)
    for line in lines:
        surface.drawString(inch, y, line)
        y -= 15
    return y - 8


def _grid(surface: Canvas, y: float, rows: list[list[str]], width: float = 1.4 * inch) -> float:
    surface.setFont("Helvetica", 10)
    for index, row in enumerate(rows):
        x = inch
        surface.setFont("Helvetica-Bold" if index == 0 else "Helvetica", 10)
        for cell in row:
            surface.rect(x, y, width, 20)
            surface.drawString(x + 4, y + 6, cell)
            x += width
        y -= 20
    # Captions must sit close to the block they describe so the parser can relate them.
    return y - 2


def _running(surface: Canvas, header: str, footer: str, page: int) -> None:
    surface.setFont("Helvetica-Oblique", 8)
    surface.drawString(inch, HEIGHT - 0.6 * inch, header)
    surface.drawString(inch, 0.55 * inch, f"{footer} | page {page}")


def basic_text(path: Path) -> None:
    surface = _new(path)
    y = _heading(surface, HEIGHT - 1.2 * inch, "Synthetic Structural Fixture", 18)
    y = _heading(surface, y, "1. Introduction")
    _body(
        surface,
        y,
        [
            "This paragraph exists to verify that plain body text is extracted intact.",
            "It contains no medical claim and no real dosage information.",
        ],
    )
    surface.save()


def multi_page(path: Path) -> None:
    surface = _new(path)
    for page in range(1, 4):
        _running(surface, "Synthetic Parsing Handbook", "Educational fixture", page)
        y = _heading(surface, HEIGHT - 1.4 * inch, f"Chapter {page}: Structure", 16)
        y = _heading(surface, y, f"{page}.1 Section heading")
        _body(
            surface,
            y,
            [
                f"Body content for chapter {page}, used to check page boundaries.",
                "Reading order must keep this line after its own heading.",
                f"Unique marker for page {page}: ALPHA-{page}-OMEGA.",
            ],
        )
        surface.showPage()
    surface.save()


def table_document(path: Path) -> None:
    surface = _new(path)
    y = _heading(surface, HEIGHT - 1.2 * inch, "Tabular Structure Fixture", 16)
    y = _body(surface, y, ["Table 1 lists synthetic values with no clinical meaning."])
    y = _grid(
        surface,
        y - 60,
        [
            ["Parameter", "Group A", "Group B", "Units"],
            ["Alpha", "10", "20", "mL"],
            ["Beta", "30", "40", "mL"],
            ["Gamma", "50", "60", "mL"],
        ],
    )
    surface.setFont("Helvetica", 10)
    surface.drawString(inch, y, "Table 1. Synthetic parameter table caption.")
    surface.save()


def continued_table(path: Path) -> None:
    surface = _new(path)
    y = _heading(surface, HEIGHT - 1.2 * inch, "Continued Table Fixture", 16)
    y = _grid(
        surface,
        y - 100,
        [["Row", "Value A", "Value B"], ["1", "11", "12"], ["2", "21", "22"]],
    )
    surface.setFont("Helvetica", 10)
    surface.drawString(inch, y, "Table 1. First part of a synthetic split table.")
    surface.showPage()
    y = _body(surface, HEIGHT - 1.2 * inch, ["Table 1 (continued)"])
    _grid(surface, y - 80, [["3", "31", "32"], ["4", "41", "42"]])
    surface.save()


def _diagram() -> Image.Image:
    """A raster diagram, as a real book figure would be: shapes only, no medical content."""
    image = Image.new("RGB", (640, 400), (240, 244, 250))
    draw = ImageDraw.Draw(image)
    draw.rectangle([40, 40, 600, 360], outline=(30, 60, 120), width=4)
    draw.ellipse([90, 110, 290, 300], fill=(60, 110, 190))
    draw.rectangle([340, 140, 560, 270], fill=(180, 90, 60))
    draw.line([290, 205, 340, 205], fill=(20, 20, 20), width=6)
    draw.text((150, 330), "SYNTHETIC DIAGRAM", fill=(20, 20, 20))
    return image


def figure_document(path: Path) -> None:
    surface = _new(path)
    _heading(surface, HEIGHT - 1.2 * inch, "Figure and Caption Fixture", 16)
    surface.setFont("Helvetica", 11)
    surface.drawString(
        inch, HEIGHT - 1.7 * inch, "The image below is a synthetic diagram, not a scan."
    )
    top = HEIGHT - 2.2 * inch
    width, height = 3.6 * inch, 2.25 * inch
    surface.drawImage(ImageReader(_diagram()), (WIDTH - width) / 2, top - height, width, height)
    surface.setFont("Helvetica", 9)
    surface.drawCentredString(
        WIDTH / 2, top - height - 14, "Figure 1. Synthetic diagram caption for parser testing."
    )
    surface.setFont("Helvetica", 11)
    surface.drawString(inch, top - height - 52, "Body text after the figure block continues here.")
    surface.save()


def formula_document(path: Path) -> None:
    """A displayed equation drawn the way a typeset book sets one: fraction rule and number."""
    surface = _new(path)
    _heading(surface, HEIGHT - 1.2 * inch, "Formula Fixture", 16)
    surface.setFont("Helvetica", 11)
    surface.drawString(
        inch,
        HEIGHT - 1.7 * inch,
        "Equation 1 defines a synthetic quantity used only for parser testing.",
    )
    y = HEIGHT - 2.6 * inch
    surface.setFont("Times-Italic", 20)
    surface.drawString(2.6 * inch, y, "C")
    surface.setFont("Times-Roman", 20)
    surface.drawString(2.9 * inch, y, "=")
    surface.setFont("Times-Italic", 20)
    surface.drawString(3.35 * inch, y + 10, "A · B")
    surface.setLineWidth(1)
    surface.line(3.3 * inch, y + 4, 4.15 * inch, y + 4)
    surface.setFont("Times-Italic", 20)
    surface.drawString(3.65 * inch, y - 16, "D")
    surface.setFont("Times-Roman", 11)
    surface.drawString(6.4 * inch, y, "(1)")
    surface.setFont("Helvetica", 11)
    surface.drawString(
        inch, HEIGHT - 3.7 * inch, "Where A, B and D are synthetic quantities with no meaning."
    )
    surface.save()


def two_column(path: Path) -> None:
    """Two narrow columns with a wide gutter, so reading order is column-wise, not row-wise."""
    surface = _new(path)
    _heading(surface, HEIGHT - 1.0 * inch, "Two Column Reading Order", 16)
    left = [
        "LEFT-1 opens the left column of this",
        "synthetic page and continues across",
        "several wrapped lines so the column",
        "forms one clearly separated block.",
        "LEFT-2 keeps the left column going",
        "with further filler wording that has",
        "no clinical content whatsoever.",
        "LEFT-3 closes the left column here.",
    ]
    right = [
        "RIGHT-1 opens the right column of the",
        "same synthetic page and also runs on",
        "for several wrapped lines to give the",
        "layout model a second clear block.",
        "RIGHT-2 keeps the right column going",
        "with further filler wording that has",
        "no clinical content whatsoever.",
        "RIGHT-3 closes the right column here.",
    ]
    surface.setFont("Helvetica", 10)
    for column_x, lines in ((inch, left), (4.5 * inch, right)):
        y = HEIGHT - 1.6 * inch
        for line in lines:
            surface.drawString(column_x, y, line)
            y -= 14
    surface.save()


def question_bank(path: Path) -> None:
    surface = _new(path)
    y = _heading(surface, HEIGHT - 1.2 * inch, "Synthetic Assessment Layout", 16)
    _body(
        surface,
        y,
        [
            "1. Which synthetic option is labelled as correct in this fixture?",
            "A. First synthetic option",
            "B. Second synthetic option",
            "C. Third synthetic option",
            "D. Fourth synthetic option",
            "Answer: B",
            "Explanation: This fixture only tests structural cue preservation.",
        ],
    )
    surface.save()


def scanned_like(path: Path) -> None:
    """A page whose content exists only as pixels, so it has no extractable text layer at all.

    The words are rasterized into an image and the image is placed on the page, exactly as a
    scanned book page behaves. `source_text_chars` is therefore zero and any recovered text
    must come from OCR.
    """
    surface = _new(path)
    page = Image.new("RGB", (1700, 2200), (252, 252, 250))
    draw = ImageDraw.Draw(page)
    lines = [
        "SCANNED PAGE SIMULATION",
        "",
        "This page has no text layer at all.",
        "Every word here exists only as pixels.",
        "Optical character recognition must",
        "recover this synthetic content.",
    ]
    y = 300
    for line in lines:
        draw.text((220, y), line, fill=(15, 15, 20))
        y += 90
    surface.drawImage(ImageReader(page), 0, 0, WIDTH, HEIGHT)
    surface.save()


def main() -> None:
    fixtures = {
        "basic-text.pdf": basic_text,
        "multi-page.pdf": multi_page,
        "table.pdf": table_document,
        "continued-table.pdf": continued_table,
        "figure.pdf": figure_document,
        "formula.pdf": formula_document,
        "two-column.pdf": two_column,
        "question-bank.pdf": question_bank,
        "scanned-like.pdf": scanned_like,
    }
    for name, builder in fixtures.items():
        builder(ROOT / name)
    (ROOT / "corrupt.pdf").write_bytes(
        b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF"
    )
    print(f"Created {len(fixtures) + 1} synthetic parsing fixtures in {ROOT}")


if __name__ == "__main__":
    main()

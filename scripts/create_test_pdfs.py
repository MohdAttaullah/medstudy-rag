"""Generate small original synthetic PDFs. No commercial or medical source material."""

from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

root = Path(__file__).resolve().parents[1] / "backend/tests/fixtures"
root.mkdir(exist_ok=True)


def write_pdf(path: Path, label: str) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 30 250 Td ({label}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_metadata({"/Title": label, "/Author": "Medical RAG synthetic test fixtures"})
    with path.open("wb") as output:
        writer.write(output)


write_pdf(root / "valid.pdf", "Synthetic educational upload fixture - edition one")
write_pdf(root / "edition-two.pdf", "Synthetic educational upload fixture - edition two")
(root / "duplicate.pdf").write_bytes((root / "valid.pdf").read_bytes())
(root / "malformed.pdf").write_bytes(b"%PDF-1.7\nnot a document\n%%EOF")
(root / "empty.pdf").write_bytes(b"")
(root / "wrong-content.pdf").write_bytes(b"MZ synthetic executable signature - not executable code")
(root / "wrong-extension.txt").write_bytes((root / "valid.pdf").read_bytes())
print("Created seven safe upload fixtures.")

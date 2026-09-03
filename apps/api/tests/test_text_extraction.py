from pathlib import Path

import pytesseract
import pytest
from PIL import Image
from pypdf import PdfWriter

from apps.api.app.settings import settings
from apps.api.app.text_extraction import (
    DocumentTextExtractionError,
    OcrTextExtractor,
    PdfTextExtractor,
    TesseractImageOcr,
)


class FakeOcr:
    def __init__(self, text: str = "Invoice Number: OCR-1") -> None:
        self.text = text
        self.calls = 0

    def recognize(self, image: Image.Image) -> str:
        self.calls += 1
        return self.text


class FakeRenderer:
    def __init__(self, image: Image.Image) -> None:
        self.image = image

    def render(self, document_path: Path):
        yield self.image


class FailingOcr:
    def recognize(self, image: Image.Image) -> str:
        raise RuntimeError("engine unavailable")


def test_ocr_extracts_valid_jpeg_without_tesseract() -> None:
    image = Image.new("RGB", (10, 10), "white")
    ocr = FakeOcr("Invoice Number: JPEG-1")
    path = Path(__file__).parent / "_ocr_jpeg_test.jpg"
    image.save(path)
    try:
        assert OcrTextExtractor(ocr=ocr).extract(path) == "Invoice Number: JPEG-1"
    finally:
        path.unlink(missing_ok=True)


def test_ocr_extracts_valid_png_and_webp(tmp_path: Path) -> None:
    for suffix in (".png", ".webp"):
        path = tmp_path / f"invoice{suffix}"
        Image.new("RGB", (10, 10), "white").save(path)
        assert OcrTextExtractor(ocr=FakeOcr("OCR text")).extract(path) == "OCR text"


def test_ocr_normalizes_transparent_png_to_rgb(tmp_path: Path) -> None:
    class ModeRecordingOcr(FakeOcr):
        mode: str | None = None

        def recognize(self, image: Image.Image) -> str:
            self.mode = image.mode
            return super().recognize(image)

    path = tmp_path / "transparent.png"
    Image.new("RGBA", (10, 10), (255, 255, 255, 0)).save(path)
    ocr = ModeRecordingOcr("PNG text")

    assert OcrTextExtractor(ocr=ocr).extract(path) == "PNG text"
    assert ocr.mode == "RGB"


def test_ocr_corrupt_image_has_meaningful_failure(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.jpg"
    path.write_bytes(b"not a valid JPEG")

    with pytest.raises(DocumentTextExtractionError, match="corrupt or uses an unsupported"):
        OcrTextExtractor(ocr=FakeOcr()).extract(path)


def test_pdf_text_extractor_falls_back_to_fake_ocr_for_scanned_pdf(tmp_path: Path) -> None:
    path = tmp_path / "scanned.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as pdf_file:
        writer.write(pdf_file)
    ocr = FakeOcr("Invoice Number: SCANNED-1")

    class FakePdfOcr:
        def extract(self, document_path: Path) -> str:
            return ocr.text

    assert PdfTextExtractor(FakePdfOcr()).extract(path) == "Invoice Number: SCANNED-1"


def test_ocr_empty_result_is_a_controlled_failure(tmp_path: Path) -> None:
    path = tmp_path / "empty.png"
    Image.new("RGB", (10, 10), "white").save(path)

    with pytest.raises(DocumentTextExtractionError, match="no extractable text"):
        OcrTextExtractor(ocr=FakeOcr("   ")).extract(path)


def test_ocr_runtime_failure_is_a_controlled_failure(tmp_path: Path) -> None:
    path = tmp_path / "failure.png"
    Image.new("RGB", (10, 10), "white").save(path)

    with pytest.raises(DocumentTextExtractionError, match="OCR document rendering failed"):
        OcrTextExtractor(ocr=FailingOcr()).extract(path)


def test_pdf_ocr_uses_fake_renderer_and_ocr(tmp_path: Path) -> None:
    path = tmp_path / "scanned.pdf"
    path.write_bytes(b"pdf")
    ocr = FakeOcr("Rendered page text")
    extractor = OcrTextExtractor(
        renderer=FakeRenderer(Image.new("RGB", (10, 10), "white")),
        ocr=ocr,
    )

    assert extractor.extract(path) == "Rendered page text"
    assert ocr.calls == 1


def test_tesseract_path_is_configurable(monkeypatch, tmp_path: Path) -> None:
    configured = tmp_path / "tesseract.exe"
    monkeypatch.setattr(
        "apps.api.app.text_extraction.pytesseract.image_to_string",
        lambda image, **kwargs: "text",
    )
    adapter = TesseractImageOcr(configured)

    assert adapter.recognize(Image.new("RGB", (1, 1))) == "text"
    assert pytesseract.pytesseract.tesseract_cmd == str(configured)


def test_image_pixel_limit_is_enforced_before_ocr(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "large.png"
    Image.new("RGB", (11, 10), "white").save(path)
    monkeypatch.setattr(settings, "max_image_pixels", 100)

    with pytest.raises(DocumentTextExtractionError, match="maximum allowed pixel count"):
        OcrTextExtractor(ocr=FakeOcr()).extract(path)


def test_pdf_page_limit_is_enforced(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "many-pages.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as pdf_file:
        writer.write(pdf_file)
    monkeypatch.setattr(settings, "max_pdf_pages", 1)

    with pytest.raises(DocumentTextExtractionError, match="maximum allowed page count"):
        PdfTextExtractor(OcrTextExtractor(ocr=FakeOcr())).extract(path)


def test_tesseract_timeout_is_reported_safely(monkeypatch) -> None:
    def time_out(image, **kwargs):
        raise RuntimeError("timeout details")

    monkeypatch.setattr("apps.api.app.text_extraction.pytesseract.image_to_string", time_out)

    with pytest.raises(DocumentTextExtractionError, match="configured timeout"):
        TesseractImageOcr().recognize(Image.new("RGB", (1, 1)))

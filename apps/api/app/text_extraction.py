from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

import pypdfium2
import pytesseract
from PIL import Image, ImageOps, UnidentifiedImageError
from pypdf import PdfReader

from apps.api.app.settings import settings


class DocumentTextExtractionError(ValueError):
    pass


class DocumentTextExtractor(Protocol):
    def extract(self, document_path: Path) -> str: ...


class PdfRenderer(Protocol):
    def render(self, document_path: Path) -> Iterable[Image.Image]: ...


class ImageOcr(Protocol):
    def recognize(self, image: Image.Image) -> str: ...


class PdfTextExtractor:
    def __init__(self, ocr_extractor: DocumentTextExtractor) -> None:
        self.ocr_extractor = ocr_extractor

    def extract(self, document_path: Path) -> str:
        try:
            reader = PdfReader(str(document_path))
            if len(reader.pages) > settings.max_pdf_pages:
                raise DocumentTextExtractionError("PDF exceeds the maximum allowed page count.")
            text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
        except DocumentTextExtractionError:
            raise
        except Exception as error:
            raise DocumentTextExtractionError("PDF text extraction failed.") from error
        if text:
            return text
        return self.ocr_extractor.extract(document_path)


class PypdfiumRenderer:
    def render(self, document_path: Path) -> Iterable[Image.Image]:
        pdf = pypdfium2.PdfDocument(str(document_path))
        try:
            if len(pdf) > settings.max_pdf_pages:
                raise DocumentTextExtractionError("PDF exceeds the maximum allowed page count.")
            for index in range(len(pdf)):
                page = pdf[index]
                bitmap = page.render(scale=int(settings.ocr_render_scale))
                try:
                    yield bitmap.to_pil()
                finally:
                    bitmap.close()
                    page.close()
        finally:
            pdf.close()


class TesseractImageOcr:
    def __init__(self, executable: Path | None = None) -> None:
        if executable is not None:
            pytesseract.pytesseract.tesseract_cmd = str(executable)

    def recognize(self, image: Image.Image) -> str:
        try:
            return pytesseract.image_to_string(image, timeout=settings.ocr_timeout_seconds)
        except pytesseract.TesseractNotFoundError as error:
            raise DocumentTextExtractionError(
                "OCR engine is unavailable; Tesseract is not installed or configured."
            ) from error
        except pytesseract.TesseractError as error:
            raise DocumentTextExtractionError(
                f"OCR engine failed with exit code {error.status}."
            ) from error
        except RuntimeError as error:
            raise DocumentTextExtractionError(
                "OCR processing exceeded the configured timeout."
            ) from error
        except Exception as error:
            raise DocumentTextExtractionError(
                "OCR engine failed to recognize the document."
            ) from error


class OcrTextExtractor:
    def __init__(
        self,
        renderer: PdfRenderer | None = None,
        ocr: ImageOcr | None = None,
    ) -> None:
        self.renderer = renderer or PypdfiumRenderer()
        self.ocr = ocr or TesseractImageOcr(settings.tesseract_executable)

    def extract(self, document_path: Path) -> str:
        try:
            if document_path.suffix.lower() in {
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
                ".bmp",
                ".gif",
                ".tif",
                ".tiff",
            }:
                try:
                    with Image.open(document_path) as image:
                        if image.width * image.height > settings.max_image_pixels:
                            raise DocumentTextExtractionError(
                                "Image exceeds the maximum allowed pixel count."
                            )
                        # Decode while the file handle is open, respect camera
                        # orientation, and flatten palette/alpha modes to the RGB
                        # input consistently accepted by Tesseract.
                        image.load()
                        normalized_image = ImageOps.exif_transpose(image).convert("RGB")
                        text = self.ocr.recognize(normalized_image)
                except UnidentifiedImageError as error:
                    raise DocumentTextExtractionError(
                        "Image file is corrupt or uses an unsupported format."
                    ) from error
            else:
                text = "\n".join(
                    self.ocr.recognize(image) for image in self.renderer.render(document_path)
                )
        except DocumentTextExtractionError:
            raise
        except Exception as error:
            raise DocumentTextExtractionError("OCR document rendering failed.") from error
        if not text.strip():
            raise DocumentTextExtractionError("OCR produced no extractable text.")
        return text.strip()

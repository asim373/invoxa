from pathlib import Path

from apps.api.app.settings import Settings


def test_tesseract_uses_path_discovery_by_default() -> None:
    configured = Settings.model_validate({})

    assert configured.tesseract_executable is None


def test_tesseract_executable_can_be_overridden(monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_EXECUTABLE", "/opt/ocr/tesseract")

    configured = Settings.model_validate({"tesseract_executable": "/opt/ocr/tesseract"})

    assert configured.tesseract_executable == Path("/opt/ocr/tesseract")

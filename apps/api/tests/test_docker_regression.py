import io
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_INTEGRATION") != "1",
    reason="requires the Docker Compose API and worker",
)

API_URL = os.getenv("DOCKER_INTEGRATION_API_URL", "http://localhost:8000")


def _image_bytes(format_name: str) -> bytes:
    image = Image.new("RGB", (1400, 900), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=52)
    lines = (
        "INVOICE",
        "Invoice Number: REG-2026-1",
        "Vendor: Regression Test",
        "Subtotal: 100.00",
        "Tax: 20.00",
        "Total: 120.00",
    )
    for index, line in enumerate(lines):
        draw.text((80, 60 + index * 125), line, fill="black", font=font)
    output = io.BytesIO()
    image.save(output, format=format_name)
    return output.getvalue()


def _pdf_bytes() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
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
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 18 Tf 40 700 Td (Invoice Number: PDF-REG Total: 120.00) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@contextmanager
def _client() -> Iterator[httpx.Client]:
    email = f"docker-regression-{uuid.uuid4()}@example.com"
    credentials = {"email": email, "password": "DockerRegression123!"}
    with httpx.Client(base_url=API_URL, timeout=15, headers={"Host": "localhost"}) as client:
        assert client.post("/auth/register", json=credentials).status_code == 201
        token = (
            client.post("/auth/login", json=credentials).raise_for_status().json()["access_token"]
        )
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


def _upload(client: httpx.Client, name: str, content: bytes, mime_type: str) -> str:
    response = client.post("/documents", files={"file": (name, content, mime_type)})
    response.raise_for_status()
    payload = response.json()
    assert payload["status"] == "queued"
    return payload["id"]


def _wait_for_completion(client: httpx.Client, document_id: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        payload = client.get(f"/documents/{document_id}").raise_for_status().json()
        if payload["status"] == "completed":
            assert payload["extracted_text"]
            return
        if payload["status"] == "failed":
            pytest.fail(f"document {document_id} failed: {payload}")
        time.sleep(0.1)
    pytest.fail(f"document {document_id} did not independently reach a terminal state")


def test_pdf_jpg_png_each_complete_before_next_upload() -> None:
    with _client() as client:
        cases = (
            ("pdf-a.pdf", _pdf_bytes(), "application/pdf"),
            ("jpg-b.jpg", _image_bytes("JPEG"), "image/jpeg"),
            ("png-c.png", _image_bytes("PNG"), "image/png"),
        )
        for name, content, mime_type in cases:
            document_id = _upload(client, name, content, mime_type)
            _wait_for_completion(client, document_id)


def test_rapid_uploads_do_not_lose_jobs() -> None:
    with _client() as client:
        documents = [
            _upload(client, f"rapid-{index}.pdf", _pdf_bytes(), "application/pdf")
            for index in range(8)
        ]
        for document_id in documents:
            _wait_for_completion(client, document_id)


def test_cross_user_document_isolation_and_owner_workflow() -> None:
    with _client() as owner, _client() as other_user:
        document_id = _upload(owner, "private-invoice.pdf", _pdf_bytes(), "application/pdf")
        _wait_for_completion(owner, document_id)

        assert owner.get(f"/documents/{document_id}").status_code == 200
        assert owner.get(f"/documents/{document_id}/file").status_code == 200
        assert owner.get(f"/documents/{document_id}/exports/csv").status_code == 200
        assert owner.get(f"/documents/{document_id}/exports/xlsx").status_code == 200

        denied_requests = (
            other_user.get(f"/documents/{document_id}"),
            other_user.get(f"/documents/{document_id}/file"),
            other_user.get(f"/documents/{document_id}/exports/csv"),
            other_user.get(f"/documents/{document_id}/exports/xlsx"),
            other_user.post(f"/documents/{document_id}/reprocess"),
            other_user.delete(f"/documents/{document_id}"),
        )
        assert {response.status_code for response in denied_requests} == {404}
        assert all("private-invoice.pdf" not in response.text for response in denied_requests)

        other_documents = other_user.get("/documents").raise_for_status().json()["items"]
        listed_ids = {item["id"] for item in other_documents}
        assert document_id not in listed_ids

        deadline = time.monotonic() + 15
        analysis: dict[str, object] = {}
        while time.monotonic() < deadline:
            response = owner.get(f"/documents/{document_id}/ai-analysis")
            response.raise_for_status()
            analysis = response.json()
            if analysis["analysis_status"] == "completed":
                break
            time.sleep(0.1)
        assert analysis["analysis_status"] == "completed"
        findings = analysis["findings"]
        assert isinstance(findings, list) and findings
        finding = findings[0]
        assert finding["explanation"]

        reviewed = owner.patch(
            f"/ai-analysis/findings/{finding['id']}", json={"status": "acknowledged"}
        )
        reviewed.raise_for_status()
        assert reviewed.json()["status"] == "acknowledged"

        analytics = owner.get("/analytics/summary?range=all").raise_for_status().json()
        assert analytics["kpis"]["total_documents"] >= 1
        assert analytics["kpis"]["ai_findings"] >= 1
        for report_type in ("financial", "ai-analysis", "processing-quality"):
            assert owner.get(f"/reports/{report_type}?range=all").status_code == 200
        for export_format in ("csv", "xlsx", "json"):
            exported = owner.get(f"/reports/ai-analysis/exports/{export_format}?range=all")
            assert exported.status_code == 200
            assert "attachment" in exported.headers["content-disposition"]

        assert other_user.get(f"/documents/{document_id}/ai-analysis").status_code == 404
        assert (
            other_user.patch(
                f"/ai-analysis/findings/{finding['id']}", json={"status": "resolved"}
            ).status_code
            == 404
        )
        other_analytics = other_user.get("/analytics/summary?range=all").raise_for_status().json()
        assert other_analytics["kpis"]["total_documents"] == 0
        assert other_user.get("/reports/ai-analysis?range=all").status_code == 404
        assert owner.delete(f"/documents/{document_id}").status_code == 204

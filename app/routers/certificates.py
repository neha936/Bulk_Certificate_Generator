"""Certificate endpoints: metadata lookup and PDF download."""

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import crud
from app.config import Settings, get_settings
from app.database import get_db
from app.models import CertificateStatus
from app.schemas import CertificateOut

router = APIRouter(prefix="/api/v1/certificates", tags=["certificates"])


@router.get(
    "/{certificate_id}",
    response_model=CertificateOut,
    summary="Get a single certificate's metadata and status",
    responses={404: {"description": "Certificate not found"}},
)
def get_certificate(certificate_id: uuid.UUID, db: Session = Depends(get_db)) -> CertificateOut:
    cert = crud.get_certificate(db, certificate_id)
    if cert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Certificate not found")
    return CertificateOut.model_validate(cert)


@router.get(
    "/{certificate_id}/download",
    response_class=FileResponse,
    summary="Download the generated certificate PDF",
    responses={
        200: {"content": {"application/pdf": {}}, "description": "The certificate PDF"},
        404: {"description": "Certificate or file not found"},
        409: {"description": "Certificate not generated (still pending, or failed)"},
    },
)
def download_certificate(
    certificate_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    cert = crud.get_certificate(db, certificate_id)
    if cert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Certificate not found")

    # 409 Conflict: the resource exists, but its current state can't produce a file.
    # The client can tell "wait and retry" (PENDING) from "this will never exist" (FAILED).
    if cert.status != CertificateStatus.SUCCESS or not cert.file_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": f"Certificate is not available for download (status={cert.status}).",
                "status": cert.status,
                "error_message": cert.error_message,
            },
        )

    storage_root = Path(settings.storage_dir).resolve()
    file_path = (storage_root / cert.file_path).resolve()
    # Defence in depth: only the server writes file_path, but never serve
    # anything outside the storage root (path traversal guard).
    if not file_path.is_relative_to(storage_root) or not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Certificate file not found on server")

    # FileResponse streams the file in chunks; it never loads it all into memory.
    safe_name = "".join(ch if ch.isalnum() else "_" for ch in cert.recipient_name).strip("_") or "certificate"
    return FileResponse(
        path=file_path,
        media_type="application/pdf",
        filename=f"certificate_{safe_name}_{cert.id.hex[:8]}.pdf",
    )

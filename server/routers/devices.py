"""Device registration, listing, approval and ping routes."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlmodel import Session, select

from ..database import get_session
from ..models import Device, DeviceStatus
from ..schemas import DeviceRead, DeviceRegisterIn, DeviceUpdate
from ..security import require_admin

router = APIRouter(prefix="/api", tags=["devices"])


@router.post(
    "/register",
    response_model=DeviceRead,
    status_code=status.HTTP_201_CREATED,
    summary="Player self-registration",
)
def register_device(
    payload: DeviceRegisterIn,
    session: Annotated[Session, Depends(get_session)],
) -> Device:
    """Players call this on first boot to announce themselves.

    Idempotent on `mac_address`: if the MAC is already known, returns the
    existing device record instead of raising. The device stays in `pending`
    until an admin approves it from the CMS.
    """
    existing = session.exec(
        select(Device).where(Device.mac_address == payload.mac_address)
    ).first()
    if existing:
        if payload.hardware_id and not existing.hardware_id:
            existing.hardware_id = payload.hardware_id
            session.add(existing)
            session.commit()
            session.refresh(existing)
        return existing

    device = Device(
        mac_address=payload.mac_address,
        hardware_id=payload.hardware_id,
        name=payload.name or f"Player-{payload.mac_address[-5:]}",
        status=DeviceStatus.pending,
    )
    session.add(device)
    session.commit()
    session.refresh(device)
    return device


@router.post("/devices/{device_id}/ping", response_model=DeviceRead)
def ping_device(
    device_id: UUID, session: Annotated[Session, Depends(get_session)]
) -> Device:
    device = session.get(Device, device_id)
    if not device:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Device not found")
    device.last_ping = datetime.utcnow()
    if device.status == DeviceStatus.offline:
        device.status = DeviceStatus.active
    session.add(device)
    session.commit()
    session.refresh(device)
    return device


@router.get("/devices", response_model=list[DeviceRead])
def list_devices(
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> list[Device]:
    return list(session.exec(select(Device).order_by(Device.registered_at.desc())))


@router.get("/devices/{device_id}", response_model=DeviceRead)
def get_device(
    device_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> Device:
    device = session.get(Device, device_id)
    if not device:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Device not found")
    return device


@router.patch("/devices/{device_id}", response_model=DeviceRead)
def update_device(
    device_id: UUID,
    payload: DeviceUpdate,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> Device:
    device = session.get(Device, device_id)
    if not device:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Device not found")

    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(device, key, value)

    session.add(device)
    session.commit()
    session.refresh(device)
    return device


@router.delete(
    "/devices/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_device(
    device_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> Response:
    device = session.get(Device, device_id)
    if not device:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Device not found")
    session.delete(device)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

"""Device registration, listing, approval, ping and token-rotation routes.

Authentication model
====================

* Admin endpoints — ``require_admin`` (JWT from ``/api/auth/login``).
* ``POST /api/register`` — unauthenticated (players use it on first boot).
  Returns a one-time cleartext ``api_token`` that the player persists.
  Idempotent on ``mac_address``: registering an already-known MAC rotates
  the token transparently, so a reinstalled kiosk regains access without
  admin intervention.
* ``POST /api/devices/{id}/ping`` — requires the device's own token.

Token rotation from the CMS invalidates all outstanding manifests.
Players transparently recover via the stale-device flow (they receive
401 on the next REST call or the next WS handshake, clear their stored
ID + token, and re-register).
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlmodel import Session, select

from ..database import get_session
from ..device_auth import device_auth_dependency, generate_api_token
from ..models import Device, DeviceStatus
from ..schemas import (
    DeviceCredentials,
    DeviceRead,
    DeviceRegisterIn,
    DeviceUpdate,
    device_to_read,
)
from ..security import require_admin

router = APIRouter(prefix="/api", tags=["devices"])


def _issue_token(device: Device) -> None:
    device.api_token = generate_api_token()
    device.api_token_issued_at = datetime.utcnow()


@router.post(
    "/register",
    response_model=DeviceCredentials,
    status_code=status.HTTP_201_CREATED,
    summary="Player self-registration",
)
def register_device(
    payload: DeviceRegisterIn,
    session: Annotated[Session, Depends(get_session)],
) -> Device:
    """Called by players on first boot, or after their stored token is
    rejected. Always returns a cleartext ``api_token`` that the player
    must persist locally.

    * New MAC → new device row, status ``pending``, fresh token.
    * Known MAC → existing row with a freshly rotated token. This is
      the only exception to the "rotate only from CMS" rule: a device
      that lost its token must be able to recover itself. The unique
      MAC + optional machine-ID + admin approval requirement still
      prevents impersonation.
    """
    existing = session.exec(
        select(Device).where(Device.mac_address == payload.mac_address)
    ).first()
    if existing:
        if payload.hardware_id and not existing.hardware_id:
            existing.hardware_id = payload.hardware_id
        _issue_token(existing)
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
    _issue_token(device)
    session.add(device)
    session.commit()
    session.refresh(device)
    return device


@router.post("/devices/{device_id}/ping", response_model=DeviceRead)
def ping_device(
    device_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    device: Annotated[Device, Depends(device_auth_dependency)],
) -> DeviceRead:
    device.last_ping = datetime.utcnow()
    if device.status == DeviceStatus.offline:
        device.status = DeviceStatus.active
    session.add(device)
    session.commit()
    session.refresh(device)
    return device_to_read(device)


@router.get("/devices", response_model=list[DeviceRead])
def list_devices(
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> list[DeviceRead]:
    devices = list(session.exec(select(Device).order_by(Device.registered_at.desc())))
    return [device_to_read(d) for d in devices]


@router.get("/devices/{device_id}", response_model=DeviceRead)
def get_device(
    device_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> DeviceRead:
    device = session.get(Device, device_id)
    if not device:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Device not found")
    return device_to_read(device)


@router.patch("/devices/{device_id}", response_model=DeviceRead)
def update_device(
    device_id: UUID,
    payload: DeviceUpdate,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> DeviceRead:
    device = session.get(Device, device_id)
    if not device:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Device not found")

    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(device, key, value)

    session.add(device)
    session.commit()
    session.refresh(device)
    return device_to_read(device)


@router.post(
    "/devices/{device_id}/rotate-token",
    response_model=DeviceCredentials,
    summary="Rotate a device's API token (admin only)",
)
def rotate_device_token(
    device_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> Device:
    """Invalidate the device's current token and issue a new one.

    Returns the cleartext token. Operators should copy it out of the
    CMS response if they plan to provision it manually; otherwise the
    player will simply receive 401 on its next call and re-register.
    """
    device = session.get(Device, device_id)
    if not device:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Device not found")
    _issue_token(device)
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

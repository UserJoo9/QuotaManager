"""Pydantic request/response models for the HTTP API."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class DeviceCreate(BaseModel):
    mac: str = Field(..., description="MAC address, e.g. aa:bb:cc:dd:ee:ff")
    name: str = ""
    quota_mode: str = "auto"  # 'fixed' | 'auto'
    fixed_gb: Optional[float] = None


class DeviceUpdate(BaseModel):
    name: Optional[str] = None
    quota_mode: Optional[str] = None
    fixed_gb: Optional[float] = None
    block: Optional[bool] = None  # true => admin_off, false => clear manual block


class TopUpRequest(BaseModel):
    extra_gb: float = Field(..., gt=0)


class BundleUpdate(BaseModel):
    total_gb: Optional[float] = Field(None, gt=0)
    #: 0 => never auto-reset (bundle is recharged manually mid-month).
    reset_day: Optional[int] = Field(None, ge=0, le=28)
    #: When set, adds GB to the current bundle without rolling the period.
    add_gb: Optional[float] = Field(None, gt=0)
    #: Escape hatch: "config" returns bundle ownership to config.yaml so it is
    #: re-applied on the next restart (a dashboard edit sets this to "dashboard").
    bundle_source: Optional[str] = None


class PasswordUpdate(BaseModel):
    current: str
    new: str = Field(..., min_length=4)


class LoginRequest(BaseModel):
    password: str

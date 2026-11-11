"""Guest approval-state policy shared by the database, service, and API."""

from __future__ import annotations

BLOCK_ADMIN = "admin_off"
BLOCK_OK = "ok"
BLOCK_GUEST_PENDING = "guest_pending"


def is_pending_guest(
        guest: bool, user_state: str | None, device_state: str | None) -> bool:
    """Return whether a guest is pending, preserving either admin block.

    A pending state on either account or device is enough to keep the guest in
    the approval flow. An explicit administrator block on either side always
    wins and cannot be cleared by a voucher.
    """
    eligible_states = (BLOCK_OK, BLOCK_GUEST_PENDING)
    return bool(
        guest
        and user_state in eligible_states
        and device_state in eligible_states
        and BLOCK_ADMIN not in (user_state, device_state)
        and BLOCK_GUEST_PENDING in (user_state, device_state)
    )

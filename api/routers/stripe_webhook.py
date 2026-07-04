"""
Stripe webhook receiver — maps subscription lifecycle events to tenant billing
state. Signature-verified (not bearer-authed). Idempotent: an event is only
recorded after it is successfully handled, so a transient failure is safely
retried by Stripe.

Link a Stripe customer/subscription to a guild by passing the guild id as the
Checkout Session ``client_reference_id`` (or as ``metadata.guild_id`` on the
subscription). Both are handled below.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg_pool
from fastapi import APIRouter, Depends, HTTPException, Request

import db.tenancy as tenancy
from api.deps import get_pool, get_settings
from utils.pg_listener import CHANNEL_CONFIG_CHANGED, emit
from utils.settings import ApiSettings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/stripe", tags=["stripe"])

# Stripe subscription.status -> our tenant.status
_STATUS_MAP = {
    "trialing": "trialing",
    "active": "active",
    "past_due": "past_due",
    "unpaid": "past_due",
    "canceled": "canceled",
    "incomplete": "past_due",
    "incomplete_expired": "canceled",
    "paused": "suspended",
}


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _resolve_guild_id(pool, obj: dict[str, Any]) -> Optional[int]:
    metadata = obj.get("metadata") or {}
    gid = _int_or_none(metadata.get("guild_id") or obj.get("client_reference_id"))
    if gid is not None and await tenancy.get_tenant(pool, gid) is not None:
        return gid

    obj_type = obj.get("object")
    sub_id = obj.get("id") if obj_type == "subscription" else obj.get("subscription")
    if sub_id:
        tenant = await tenancy.find_tenant_by_subscription(pool, sub_id)
        if tenant:
            return tenant["guild_id"]

    customer = obj.get("customer")
    if customer:
        tenant = await tenancy.find_tenant_by_customer(pool, customer)
        if tenant:
            return tenant["guild_id"]
    return None


def _plan_for_subscription(obj: dict[str, Any], settings: ApiSettings) -> Optional[str]:
    try:
        price_id = obj["items"]["data"][0]["price"]["id"]
    except (KeyError, IndexError, TypeError):
        return None
    return settings.stripe_price_plans.get(price_id)


async def _handle_subscription(pool, settings, obj: dict[str, Any], *, deleted: bool = False) -> Optional[int]:
    guild_id = await _resolve_guild_id(pool, obj)
    if guild_id is None:
        logger.warning("Stripe subscription event could not be matched to a tenant.")
        return None

    status = "canceled" if deleted else _STATUS_MAP.get(obj.get("status", ""), "past_due")
    cpe = obj.get("current_period_end")
    await tenancy.apply_subscription_state(
        pool,
        guild_id,
        status=status,
        plan=_plan_for_subscription(obj, settings),
        stripe_customer_id=obj.get("customer"),
        stripe_subscription_id=obj.get("id"),
        current_period_end=datetime.fromtimestamp(cpe, tz=timezone.utc) if cpe else None,
    )
    return guild_id


async def _handle_checkout_completed(pool, obj: dict[str, Any]) -> Optional[int]:
    guild_id = await _resolve_guild_id(pool, obj)
    if guild_id is None:
        logger.warning("Stripe checkout.session.completed could not be matched to a tenant.")
        return None
    await tenancy.apply_subscription_state(
        pool,
        guild_id,
        status="active",
        stripe_customer_id=obj.get("customer"),
        stripe_subscription_id=obj.get("subscription"),
    )
    return guild_id


async def _handle_invoice(pool, obj: dict[str, Any], *, paid: bool) -> Optional[int]:
    guild_id = await _resolve_guild_id(pool, obj)
    if guild_id is None:
        return None
    await tenancy.apply_subscription_state(pool, guild_id, status="active" if paid else "past_due")
    return guild_id


@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    pool: psycopg_pool.AsyncConnectionPool = Depends(get_pool),
    settings: ApiSettings = Depends(get_settings),
) -> dict[str, Any]:
    if not settings.stripe_webhook_secret:
        raise HTTPException(status_code=503, detail="Stripe is not configured (STRIPE_WEBHOOK_SECRET unset).")

    try:
        import stripe
    except ImportError:
        raise HTTPException(status_code=503, detail="stripe library not installed.")

    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, settings.stripe_webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="Invalid Stripe signature.")

    event_id = event["id"]
    event_type = event["type"]

    if await tenancy.has_stripe_event(pool, event_id):
        return {"status": "duplicate", "event": event_type}

    obj = event["data"]["object"]
    guild_id: Optional[int] = None

    if event_type == "checkout.session.completed":
        guild_id = await _handle_checkout_completed(pool, obj)
    elif event_type in ("customer.subscription.created", "customer.subscription.updated"):
        guild_id = await _handle_subscription(pool, settings, obj)
    elif event_type == "customer.subscription.deleted":
        guild_id = await _handle_subscription(pool, settings, obj, deleted=True)
    elif event_type == "invoice.paid":
        guild_id = await _handle_invoice(pool, obj, paid=True)
    elif event_type == "invoice.payment_failed":
        guild_id = await _handle_invoice(pool, obj, paid=False)
    else:
        logger.info("Ignoring unhandled Stripe event: %s", event_type)

    # Only mark processed after successful handling (idempotency + safe retry).
    await tenancy.record_stripe_event(pool, event_id, event_type)

    if guild_id is not None:
        # Billing state changed serviceability — nudge the bot to re-evaluate.
        await emit(pool, CHANNEL_CONFIG_CHANGED, str(guild_id))

    return {"status": "processed", "event": event_type, "guild_id": guild_id}

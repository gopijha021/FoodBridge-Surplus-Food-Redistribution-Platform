"""Email/SMS/push provider stub. Real providers go here; always use timeouts and bounded retries
(retries are driven by Notification.retry_count in app.workers.consumers)."""
import logging

log = logging.getLogger("foodbridge.notifier")
TIMEOUT_SECONDS = 5


def send(user_email: str, phone: str | None, event_type: str, payload: dict) -> None:
    log.info("notification sent", extra={"extra_fields": {"to": user_email, "event_type": event_type, "payload": payload}})

"""Send one deliberately configured, backend-only WhatsApp template test.

Run this only from a trusted operator shell where the Container App secrets are
available.  It is not exposed as a public HTTP route.
"""

from __future__ import annotations

import os

from services.whatsapp_service import (
    WhatsAppConfigurationError,
    send_template_message,
    sender_settings_from_environment,
)


def required_setting(name: str) -> str:
    """Read a non-empty test-only setting without printing its value."""
    value = os.getenv(name, "").strip()
    if not value:
        raise WhatsAppConfigurationError(f"{name} is not configured")
    return value


def main() -> None:
    """Send the configured safe test and print only Meta's message identifier."""
    result = send_template_message(
        sender_settings_from_environment(),
        required_setting("WHATSAPP_TEST_RECIPIENT"),
        required_setting("WHATSAPP_TEMPLATE_NAME"),
        os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "en_US"),
        (
            os.getenv("WHATSAPP_TEST_PARAMETER_1", "Test"),
            os.getenv("WHATSAPP_TEST_PARAMETER_2", "Tuesday, 14 October at 10:20 AM"),
        ),
        (os.getenv("WHATSAPP_TEST_QUICK_REPLY_PAYLOAD", "help"),),
    )
    message_id = str(result["messages"][0].get("id", ""))
    if not message_id:
        raise WhatsAppConfigurationError("Meta did not return a message identifier")
    print(f"WhatsApp template test accepted by Meta. Message ID: {message_id}")


if __name__ == "__main__":
    main()

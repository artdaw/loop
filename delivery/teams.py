"""Teams delivery — outbound message formatting and sending.

Formats and sends reminders, follow-up approvals, and briefings to Microsoft
Teams using the Bot Framework. Inbound handling lives in
``integrations/teams_bot.py``.

Phase 2 scope (Teams lands in Phase 2 per the roadmap):
    - send(): proactively message the user in Teams.
    - send_approval(): send an Adaptive Card with approve/edit/snooze actions.
"""

from __future__ import annotations

from config.settings import Settings, get_settings
from delivery.telegram import ApprovalPrompt


class TeamsDelivery:
    """Outbound Teams sender."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._adapter = None
        # TODO(phase2): construct the Bot Framework adapter + conversation refs.

    def send(self, text: str, *, conversation_id: str | None = None) -> None:
        """Proactively send a message to the user's Teams conversation."""
        # TODO(phase2): use the stored conversation reference to send proactively.
        raise NotImplementedError("TeamsDelivery.send is a Phase 2 stub.")

    def send_approval(self, prompt: ApprovalPrompt, *,
                      conversation_id: str | None = None) -> None:
        """Send an Adaptive Card approval prompt."""
        # TODO(phase2): render an Adaptive Card from prompt.buttons.
        raise NotImplementedError("TeamsDelivery.send_approval is a Phase 2 stub.")

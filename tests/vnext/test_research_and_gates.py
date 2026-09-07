"""Research evidence and the remaining control gates — A05, A10, A18–A22."""

from __future__ import annotations

import threading
import time

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from loop.agents.seeker import (
    Citation,
    Claim,
    ClaimStatus,
    ResearchDraft,
    ReviewVerdict,
    compile_draft,
    evidence_for,
    validate_draft,
)
from loop.ai.model_gateway import ModelGateway, PrivacyError
from loop.ai.spend import (
    DailySpendLedger,
    ModelPrice,
    PriceBook,
    ReservationState,
    cloud_call_permitted,
)
from loop.core.errors import (
    AuthRequired,
    BudgetExhausted,
    Unavailable,
    ValidationFailed,
)
from loop.core.privacy import ModelScope, PrivacyLabel
from loop.core.settings import Settings
from loop.runtime.intake import EventIntake, InboundMessage
from loop.services.diagnostics import build_export, is_allowed_key, sanitize
from loop.services.export_map import ExportMap, ExportScope, RemoteLink
from loop.vault.receipts import ReceiptStore

SOURCE = b"Production lead time is six weeks for the standard configuration. " \
         b"Expedited builds are quoted separately and are not covered here. " \
         b"Historical figures from 2024 are appended below for reference only."


def _receipts(*, whole: bool = True) -> ReceiptStore:
    store = ReceiptStore(run_id="run-1")
    if whole:
        store.read_whole(source_id="s1", path="0-raw/vendor.md", body=SOURCE)
    else:
        store.read_preview(source_id="s1", path="0-raw/vendor.md", body=SOURCE,
                           preview_bytes=40)
    return store


def _settings(**kw) -> Settings:
    base = {"_env_file": None, "ollama_default_model": "llama3.1:8b"}
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


PRIVATE = PrivacyLabel(model_scope=ModelScope.LOCAL_ONLY,
                       origins=frozenset({"telegram_private"}), sensitive=True)


# --------------------------------------------------------------------------- #
# A05 — the evidence validator outranks the reviewer
# --------------------------------------------------------------------------- #
def test_a05_a_cited_claim_within_a_read_span_is_supported():
    draft = ResearchDraft(run_id="r", question="lead time?", claims=[
        Claim("Production lead time is six weeks.",
              [Citation("s1", 0, 60, path="0-raw/vendor.md")])])

    report = validate_draft(draft, receipts=_receipts())

    assert report.ok is True
    assert report.findings[0].status is ClaimStatus.SUPPORTED


def test_a05_an_uncited_claim_is_blocked():
    draft = ResearchDraft(run_id="r", question="q",
                          claims=[Claim("Lead time is two weeks.")])
    report = validate_draft(draft, receipts=_receipts())

    assert report.ok is False
    assert report.blocking[0].status is ClaimStatus.UNCITED


def test_a05_an_approving_reviewer_does_not_unblock_it():
    """Two model opinions are not evidence; the reviewer read the same fiction."""
    draft = ResearchDraft(run_id="r", question="q",
                          claims=[Claim("Lead time is two weeks.")])

    with pytest.raises(ValidationFailed) as excinfo:
        compile_draft(draft, receipts=_receipts(),
                      review=ReviewVerdict(approved=True,
                                           comment="looks right to me"))

    assert excinfo.value.details["reviewer_approved"] is True


def test_a05_the_refusal_names_the_offending_claim():
    draft = ResearchDraft(run_id="r", question="q", claims=[
        Claim("Lead time is six weeks.", [Citation("s1", 0, 60)]),
        Claim("Expedited builds take three days.")])

    with pytest.raises(ValidationFailed) as excinfo:
        compile_draft(draft, receipts=_receipts(),
                      review=ReviewVerdict(approved=True))

    blocking = excinfo.value.details["blocking"]
    assert len(blocking) == 1
    assert "Expedited builds" in blocking[0]


def test_a05_a_citation_to_a_source_that_was_never_read_is_blocked():
    draft = ResearchDraft(run_id="r", question="q", claims=[
        Claim("The supplier changed hands in 2025.",
              [Citation("s99", 0, 10)])])
    report = validate_draft(draft, receipts=_receipts())

    assert report.blocking[0].status is ClaimStatus.UNREAD_SOURCE


def test_a05_a_citation_beyond_a_preview_read_is_blocked():
    """Reading the first 40 bytes cannot support a claim about byte 200."""
    draft = ResearchDraft(run_id="r", question="q", claims=[
        Claim("Historical 2024 figures are appended.",
              [Citation("s1", 150, 200)])])
    report = validate_draft(draft, receipts=_receipts(whole=False))

    assert report.blocking[0].status is ClaimStatus.UNCOVERED_SPAN


def test_a05_a_source_outside_the_compiled_set_is_blocked():
    draft = ResearchDraft(run_id="r", question="q", claims=[
        Claim("Lead time is six weeks.", [Citation("s1", 0, 60)])])
    report = validate_draft(draft, receipts=_receipts(),
                            compiled_sources=set())

    assert report.blocking[0].status is ClaimStatus.UNCOMPILED_SOURCE


def test_a05_a_fully_supported_draft_compiles():
    draft = ResearchDraft(run_id="r", question="q", claims=[
        Claim("Production lead time is six weeks.", [Citation("s1", 0, 60)])])

    assert compile_draft(draft, receipts=_receipts(),
                         review=ReviewVerdict(approved=True)) == [
        "Production lead time is six weeks."]


def test_a05_a_supported_draft_compiles_even_without_a_review():
    """The validator is the gate; a missing review is not a second one."""
    draft = ResearchDraft(run_id="r", question="q", claims=[
        Claim("Production lead time is six weeks.", [Citation("s1", 0, 60)])])

    assert compile_draft(draft, receipts=_receipts()) == [
        "Production lead time is six weeks."]


def test_a05_citations_convert_to_receipt_evidence():
    claims = [Claim("Lead time is six weeks.",
                    [Citation("s1", 0, 60, path="0-raw/vendor.md",
                              content_hash="h", quote="six weeks")])]
    evidence = evidence_for(claims)

    assert evidence[0].source_id == "s1"
    assert evidence[0].span.start == 0


def test_a05_the_receipt_store_agrees_with_the_validator():
    receipts = _receipts()
    claims = [Claim("Lead time is six weeks.",
                    [Citation("s1", 0, 60, path="0-raw/vendor.md",
                              content_hash=receipts.get("s1").content_hash)])]

    receipts.validate(evidence_for(claims))          # raises if unsupported


# --------------------------------------------------------------------------- #
# A10 — a private request when the local model is down
# --------------------------------------------------------------------------- #
class BrokenModel(GenericFakeChatModel):
    def invoke(self, *args, **kwargs):  # type: ignore[override]
        raise ConnectionError("ollama is not running")


def _broken() -> BrokenModel:
    return BrokenModel(messages=iter([AIMessage(content="never")]))


def _working(text: str = "cloud answer") -> GenericFakeChatModel:
    return GenericFakeChatModel(messages=iter([AIMessage(content=text)] * 10))


def _cloud_settings(**kw) -> Settings:
    base = {"cloud_enabled": True, "anthropic_api_key": "k",
            "anthropic_model": "claude-x", "cloud_daily_budget_usd": 5}
    base.update(kw)
    return _settings(**base)


def test_a10_a_private_request_fails_closed_when_the_local_model_is_down():
    gateway = ModelGateway(settings=_cloud_settings(), local_model=_broken(),
                           cloud_model=_working())

    with pytest.raises((PrivacyError, Unavailable)):
        gateway.invoke("private question", labels=[PRIVATE])


def test_a10_no_cloud_call_is_made_on_that_path():
    """A configured, working cloud model is right there. It is not used."""
    gateway = ModelGateway(settings=_cloud_settings(), local_model=_broken(),
                           cloud_model=_working())

    with pytest.raises((PrivacyError, Unavailable)):
        gateway.invoke("private question", labels=[PRIVATE])

    assert gateway.audit.cloud_calls == 0


def test_a10_the_same_holds_for_a_summary_of_private_context():
    gateway = ModelGateway(settings=_cloud_settings(), local_model=_broken(),
                           cloud_model=_working())

    with pytest.raises((PrivacyError, Unavailable)):
        gateway.invoke("summarise", labels=[PRIVATE], purpose="summary")

    assert gateway.audit.cloud_calls == 0


def test_a10_a_caller_cannot_downgrade_the_private_label():
    """`local_only=False` from a caller merges *with* the private label."""
    permissive = PrivacyLabel(model_scope=ModelScope.CLOUD_ALLOWED)
    gateway = ModelGateway(settings=_cloud_settings(), local_model=_broken(),
                           cloud_model=_working())

    with pytest.raises((PrivacyError, Unavailable)):
        gateway.invoke("q", labels=[PRIVATE, permissive])

    assert gateway.audit.cloud_calls == 0


# --------------------------------------------------------------------------- #
# A18 — a bulk export with no mapping
# --------------------------------------------------------------------------- #
def _export_map() -> ExportMap:
    return ExportMap(
        links=[RemoteLink(provider="wrike", account_id="acc", remote_id="W-1",
                          object_type="task", local_id="t-work")],
        scopes=[ExportScope(provider="wrike", project_ref="project:client",
                            object_types=frozenset({"task"}))])


def test_a18_a_mapped_task_is_pushed():
    decision = _export_map().decide(provider="wrike", local_id="t-work",
                                    project_ref="project:client",
                                    object_type="task")

    assert decision.push is True
    assert decision.remote_id == "W-1"


def test_a18_a_personal_task_with_no_mapping_is_not_pushed():
    decision = _export_map().decide(provider="wrike", local_id="t-personal",
                                    project_ref="project:home",
                                    object_type="task")

    assert decision.push is False
    assert "stays local" in decision.reason


def test_a18_a_bulk_instruction_is_decided_per_object():
    """"Push everything" is a statement about a project, not about every row."""
    plan = _export_map().plan_bulk_push([
        {"local_id": "t-work", "project_ref": "project:client",
         "object_type": "task"},
        {"local_id": "t-personal", "project_ref": "project:home",
         "object_type": "task"},
    ], provider="wrike")

    assert [d.push for d in plan] == [True, False]


def test_a18_an_unmapped_task_inside_an_approved_scope_may_be_created():
    decision = _export_map().decide(provider="wrike", local_id="t-new",
                                    project_ref="project:client",
                                    object_type="task")

    assert decision.push is True
    assert "scope" in decision.reason


def test_a18_a_scope_does_not_extend_to_other_object_types():
    decision = _export_map().decide(provider="wrike", local_id="n-1",
                                    project_ref="project:client",
                                    object_type="note")
    assert decision.push is False


def test_a18_a_scope_does_not_extend_to_another_provider():
    decision = _export_map().decide(provider="asana", local_id="t-new",
                                    project_ref="project:client",
                                    object_type="task")
    assert decision.push is False


# --------------------------------------------------------------------------- #
# A19 — a key is not authorisation
# --------------------------------------------------------------------------- #
def _prices() -> PriceBook:
    return PriceBook([ModelPrice("claude-x", input_usd_per_1k=0.003,
                                 output_usd_per_1k=0.015,
                                 pinned_on="2026-09-01")])


def test_a19_a_key_without_enablement_permits_no_cloud_call():
    settings = _settings(anthropic_api_key="k", anthropic_model="claude-x",
                         cloud_daily_budget_usd=5)

    assert settings.cloud_available is False
    permitted, reason = cloud_call_permitted(
        cloud_available=settings.cloud_available, local_only=False,
        ledger=DailySpendLedger(daily_limit_usd=5, prices=_prices()))
    assert permitted is False and "CLOUD_ENABLED" in reason


def test_a19_a_zero_budget_permits_no_cloud_call():
    settings = _cloud_settings(cloud_daily_budget_usd=0)
    assert settings.cloud_available is False


def test_a19_a_zero_limit_ledger_refuses():
    permitted, reason = cloud_call_permitted(
        cloud_available=True, local_only=False,
        ledger=DailySpendLedger(daily_limit_usd=0, prices=_prices()))

    assert permitted is False and "zero" in reason


def test_a19_the_gateway_makes_no_cloud_call_when_cloud_is_disabled():
    gateway = ModelGateway(
        settings=_settings(anthropic_api_key="k", anthropic_model="claude-x"),
        local_model=_working("local"), cloud_model=_working("cloud"))

    result = gateway.invoke("public question",
                            labels=[PrivacyLabel(
                                model_scope=ModelScope.CLOUD_ALLOWED)])

    assert result.backend == "local"
    assert gateway.audit.cloud_calls == 0


def test_a19_unknown_pricing_blocks_the_call():
    ledger = DailySpendLedger(daily_limit_usd=5, prices=PriceBook())

    with pytest.raises(Unavailable, match="pinned price"):
        ledger.reserve(reservation_id="r1", model_id="claude-x",
                       max_input_tokens=1000, max_output_tokens=1000)


def test_a19_private_context_vetoes_cloud_even_when_fully_configured():
    permitted, reason = cloud_call_permitted(
        cloud_available=True, local_only=True,
        ledger=DailySpendLedger(daily_limit_usd=5, prices=_prices()))

    assert permitted is False and "private" in reason


# --------------------------------------------------------------------------- #
# A20 — concurrent calls near the daily limit
# --------------------------------------------------------------------------- #
def test_a20_a_reservation_is_taken_before_the_call():
    ledger = DailySpendLedger(daily_limit_usd=1.0, prices=_prices())
    ledger.reserve(reservation_id="r1", model_id="claude-x",
                   max_input_tokens=10_000, max_output_tokens=10_000)

    assert ledger.committed_usd == pytest.approx(0.18)
    assert ledger.remaining_usd == pytest.approx(0.82)


def test_a20_a_call_exceeding_the_ceiling_is_refused():
    ledger = DailySpendLedger(daily_limit_usd=0.10, prices=_prices())

    with pytest.raises(BudgetExhausted):
        ledger.reserve(reservation_id="r1", model_id="claude-x",
                       max_input_tokens=10_000, max_output_tokens=10_000)


def test_a20_settling_frees_the_unused_estimate():
    ledger = DailySpendLedger(daily_limit_usd=1.0, prices=_prices())
    ledger.reserve(reservation_id="r1", model_id="claude-x",
                   max_input_tokens=10_000, max_output_tokens=10_000)
    ledger.settle("r1", input_tokens=1000, output_tokens=100)

    assert ledger.committed_usd == pytest.approx(0.0045)


def test_a20_concurrent_reservations_cannot_overspend(monkeypatch):
    """Twenty threads, room for four.

    The check-then-act window is widened deterministically: `Reservation.__init__`
    is constructed between reading the committed total and writing it back, so a
    sleep there is exactly the interval the lock must cover. Relying on the
    scheduler instead detects the unlocked version about a quarter of the time,
    and a test that usually passes on broken code is not a test.
    """
    import loop.ai.spend as spend

    class SlowReservation(spend.Reservation):
        def __init__(self, *args, **kwargs):
            time.sleep(0.002)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(spend, "Reservation", SlowReservation)

    ledger = DailySpendLedger(daily_limit_usd=0.80, prices=_prices())
    granted: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(20)

    def attempt(index: int) -> None:
        barrier.wait()
        try:
            ledger.reserve(reservation_id=f"r{index}", model_id="claude-x",
                           max_input_tokens=10_000, max_output_tokens=10_000)
        except BudgetExhausted:
            return
        with lock:
            granted.append(f"r{index}")

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(granted) == 4
    assert ledger.committed_usd <= 0.80


def test_a20_an_unknown_outcome_keeps_holding_the_estimate():
    """"We do not know whether we spent this" is not "we did not spend it"."""
    ledger = DailySpendLedger(daily_limit_usd=1.0, prices=_prices())
    ledger.reserve(reservation_id="r1", model_id="claude-x",
                   max_input_tokens=10_000, max_output_tokens=10_000)
    ledger.mark_unknown("r1")

    assert ledger.get("r1").state is ReservationState.UNKNOWN
    assert ledger.committed_usd == pytest.approx(0.18)


def test_a20_uncertainty_blocks_further_calls_at_the_margin():
    ledger = DailySpendLedger(daily_limit_usd=0.20, prices=_prices())
    ledger.reserve(reservation_id="r1", model_id="claude-x",
                   max_input_tokens=10_000, max_output_tokens=10_000)
    ledger.mark_unknown("r1")

    with pytest.raises(BudgetExhausted):
        ledger.reserve(reservation_id="r2", model_id="claude-x",
                       max_input_tokens=10_000, max_output_tokens=10_000)


def test_a20_a_call_that_provably_never_happened_is_released():
    ledger = DailySpendLedger(daily_limit_usd=1.0, prices=_prices())
    ledger.reserve(reservation_id="r1", model_id="claude-x",
                   max_input_tokens=10_000, max_output_tokens=10_000)
    ledger.release("r1")

    assert ledger.committed_usd == 0.0


def test_a20_a_repeated_reservation_id_does_not_double_charge():
    ledger = DailySpendLedger(daily_limit_usd=1.0, prices=_prices())
    ledger.reserve(reservation_id="r1", model_id="claude-x",
                   max_input_tokens=10_000, max_output_tokens=10_000)
    ledger.reserve(reservation_id="r1", model_id="claude-x",
                   max_input_tokens=10_000, max_output_tokens=10_000)

    assert ledger.committed_usd == pytest.approx(0.18)


def test_a20_an_exhausted_cloud_budget_does_not_affect_local_inference():
    gateway = ModelGateway(settings=_settings(), local_model=_working("local"))
    result = gateway.invoke("q", labels=[PrivacyLabel(
        model_scope=ModelScope.CLOUD_ALLOWED)])

    assert result.backend == "local"


# --------------------------------------------------------------------------- #
# A21 — diagnostics carry metadata only
# --------------------------------------------------------------------------- #
def test_a21_allowed_metadata_survives():
    assert is_allowed_key("latency_ms") is True
    assert is_allowed_key("reason_code") is True


def test_a21_content_bearing_keys_are_dropped():
    for key in ("prompt", "message_body", "profile", "chat_id", "api_key",
                "authorization"):
        assert is_allowed_key(key) is False, key


def test_a21_a_hash_of_a_prompt_is_allowed():
    """A hash identifies a call without revealing it — that is its purpose."""
    assert is_allowed_key("prompt_hash") is True


def test_a21_an_unknown_key_is_dropped_by_default():
    """The export allows named fields; it does not strip known-bad ones."""
    assert is_allowed_key("some_new_field_someone_added") is False


def test_a21_the_export_keeps_only_allowed_keys():
    export = build_export([{
        "action": "model.invoke", "decision": "allow", "latency_ms": 42,
        "prompt": "the user's private question",
        "message_body": "remind me about the biopsy results",
        "authorization": "Bearer sk-abc123def456",
    }])

    assert export.key_set == {"action", "decision", "latency_ms"}


def test_a21_the_dropped_keys_are_reported_not_silently_removed():
    export = build_export([{"action": "x", "prompt": "secret"}])
    assert export.dropped_keys == ["prompt"]


def test_a21_credentials_inside_an_allowed_field_are_masked():
    cleaned = sanitize({"reason_code": "auth failed for Bearer sk-abcdef123456"})
    assert "sk-abcdef123456" not in cleaned["reason_code"]
    assert "[redacted]" in cleaned["reason_code"]


def test_a21_an_email_address_in_an_allowed_field_is_masked():
    cleaned = sanitize({"reason_code": "delivery to gleb@example.com failed"})
    assert "gleb@example.com" not in cleaned["reason_code"]


def test_a21_no_message_body_survives_a_realistic_record():
    export = build_export([{
        "event_id": "e1", "action": "message.received", "decision": "accepted",
        "body": "I have a doctor's appointment on Thursday",
        "payload": {"text": "I have a doctor's appointment on Thursday"},
    }])
    serialised = repr(export.records)

    assert "doctor" not in serialised
    assert "appointment" not in serialised


# --------------------------------------------------------------------------- #
# A22 — an arbitrary /start from a stranger
# --------------------------------------------------------------------------- #
def _stranger() -> InboundMessage:
    return InboundMessage(
        origin="telegram", origin_id="telegram:999", idempotency_key="tg:999",
        kind="message.received", payload={"text": "/start"},
        actor_chat_id="999999", actor_user_id="888888")


def test_a22_an_unbound_instance_refuses_a_stranger(sessions, clock, settings):
    unbound = settings.model_copy(update={"telegram_chat_id": "",
                                          "telegram_user_id": ""})
    intake = EventIntake(sessions=sessions, settings=unbound, clock=clock)

    with pytest.raises(AuthRequired):
        intake.accept(_stranger())


def test_a22_a_bound_instance_refuses_a_different_sender(sessions, clock,
                                                         settings):
    intake = EventIntake(sessions=sessions, settings=settings, clock=clock)
    with pytest.raises(AuthRequired):
        intake.accept(_stranger())


def test_a22_no_event_is_stored_for_the_stranger(sessions, clock, settings):
    from sqlalchemy import text

    intake = EventIntake(sessions=sessions, settings=settings, clock=clock)
    with pytest.raises(AuthRequired):
        intake.accept(_stranger())

    with sessions() as session:
        count = session.execute(text("SELECT COUNT(*) FROM events")).scalar()
    assert count == 0


def test_a22_the_refusal_carries_no_owner_content(sessions, clock, settings):
    """A stranger learns that pairing is required, and nothing else."""
    intake = EventIntake(sessions=sessions, settings=settings, clock=clock)

    with pytest.raises(AuthRequired) as excinfo:
        intake.accept(_stranger())

    message = str(excinfo.value)
    assert settings.telegram_chat_id not in message
    assert "task" not in message.lower()


def test_a22_the_bound_owner_is_still_accepted(sessions, clock, settings):
    intake = EventIntake(sessions=sessions, settings=settings, clock=clock)
    result = intake.accept(InboundMessage(
        origin="telegram", origin_id="telegram:1", idempotency_key="tg:1",
        kind="message.received", payload={"text": "/start"},
        actor_chat_id=settings.telegram_chat_id,
        actor_user_id=settings.telegram_user_id))

    assert result.accepted is True


# --------------------------------------------------------------------------- #
# M3 — the spend ledger survives a restart (A20)
# --------------------------------------------------------------------------- #
def test_m3_a_reservation_survives_a_fresh_ledger_instance(sessions, clock):
    """A fresh process for the same day must see what was already committed,
    or the daily cloud budget can be spent twice."""
    prices = PriceBook([ModelPrice("claude-x", input_usd_per_1k=0.003,
                                   output_usd_per_1k=0.015,
                                   pinned_on="2026-09-01")])
    first = DailySpendLedger(daily_limit_usd=1.0, prices=prices, day="2026-09-07",
                             sessions=sessions, clock=clock)
    first.reserve(reservation_id="r1", model_id="claude-x",
                 max_input_tokens=10_000, max_output_tokens=10_000)

    second = DailySpendLedger(daily_limit_usd=1.0, prices=prices, day="2026-09-07",
                              sessions=sessions, clock=clock)
    assert second.committed_usd == pytest.approx(0.18)
    assert second.get("r1") is not None


def test_m3_settlement_survives_a_restart(sessions, clock):
    prices = PriceBook([ModelPrice("claude-x", input_usd_per_1k=0.003,
                                   output_usd_per_1k=0.015,
                                   pinned_on="2026-09-01")])
    first = DailySpendLedger(daily_limit_usd=1.0, prices=prices, day="2026-09-07",
                             sessions=sessions, clock=clock)
    first.reserve(reservation_id="r1", model_id="claude-x",
                 max_input_tokens=10_000, max_output_tokens=10_000)
    first.settle("r1", input_tokens=1000, output_tokens=100)

    second = DailySpendLedger(daily_limit_usd=1.0, prices=prices, day="2026-09-07",
                              sessions=sessions, clock=clock)
    assert second.get("r1").state is ReservationState.SETTLED
    assert second.committed_usd == pytest.approx(0.0045)


def test_m3_a_different_day_does_not_see_yesterdays_reservations(sessions, clock):
    """The daily ceiling resets per day; loading everything ever reserved
    would silently carry yesterday's spend into today's limit."""
    prices = PriceBook([ModelPrice("claude-x", input_usd_per_1k=0.003,
                                   output_usd_per_1k=0.015,
                                   pinned_on="2026-09-01")])
    yesterday = DailySpendLedger(daily_limit_usd=1.0, prices=prices,
                                 day="2026-09-06", sessions=sessions, clock=clock)
    yesterday.reserve(reservation_id="r1", model_id="claude-x",
                      max_input_tokens=10_000, max_output_tokens=10_000)

    today = DailySpendLedger(daily_limit_usd=1.0, prices=prices,
                             day="2026-09-07", sessions=sessions, clock=clock)
    assert today.committed_usd == 0.0


def test_m3_a_ledger_with_no_sessions_still_works_exactly_as_before(clock):
    """Every existing unit test constructs the ledger with no `sessions`."""
    prices = PriceBook([ModelPrice("claude-x", input_usd_per_1k=0.003,
                                   output_usd_per_1k=0.015,
                                   pinned_on="2026-09-01")])
    ledger = DailySpendLedger(daily_limit_usd=1.0, prices=prices)
    ledger.reserve(reservation_id="r1", model_id="claude-x",
                   max_input_tokens=10_000, max_output_tokens=10_000)
    assert ledger.committed_usd == pytest.approx(0.18)


def test_m3_an_unknown_outcome_still_holds_after_a_restart(sessions, clock):
    prices = PriceBook([ModelPrice("claude-x", input_usd_per_1k=0.003,
                                   output_usd_per_1k=0.015,
                                   pinned_on="2026-09-01")])
    first = DailySpendLedger(daily_limit_usd=1.0, prices=prices, day="2026-09-07",
                             sessions=sessions, clock=clock)
    first.reserve(reservation_id="r1", model_id="claude-x",
                 max_input_tokens=10_000, max_output_tokens=10_000)
    first.mark_unknown("r1")

    second = DailySpendLedger(daily_limit_usd=1.0, prices=prices, day="2026-09-07",
                              sessions=sessions, clock=clock)
    assert second.get("r1").state is ReservationState.UNKNOWN
    assert second.committed_usd == pytest.approx(0.18)


# --------------------------------------------------------------------------- #
# M3 — the export map survives a restart
# --------------------------------------------------------------------------- #
def test_m3_an_added_link_survives_a_fresh_export_map(sessions):
    first = ExportMap(sessions=sessions)
    first.add_link(RemoteLink(provider="wrike", account_id="acc",
                              remote_id="W-1", object_type="task",
                              local_id="t-work"))

    second = ExportMap(sessions=sessions)
    decision = second.decide(provider="wrike", local_id="t-work",
                             project_ref="project:client", object_type="task")
    assert decision.push is True
    assert decision.remote_id == "W-1"


def test_m3_an_added_scope_survives_a_restart(sessions):
    first = ExportMap(sessions=sessions)
    first.add_scope(ExportScope(provider="wrike", project_ref="project:client",
                                object_types=frozenset({"task"})))

    second = ExportMap(sessions=sessions)
    decision = second.decide(provider="wrike", local_id="t-new",
                             project_ref="project:client", object_type="task")
    assert decision.push is True
    assert "scope" in decision.reason


def test_m3_an_unmapped_object_still_stays_local_after_a_restart(sessions):
    """Losing the scope on restart would be the dangerous direction; confirm
    an object outside any persisted authority stays local either way."""
    first = ExportMap(sessions=sessions)
    first.add_scope(ExportScope(provider="wrike", project_ref="project:client"))

    second = ExportMap(sessions=sessions)
    decision = second.decide(provider="wrike", local_id="t-personal",
                             project_ref="project:home", object_type="task")
    assert decision.push is False


def test_m3_links_constructed_inline_are_also_persisted(sessions):
    """The constructor's own `links=`/`scopes=` args must not bypass storage."""
    ExportMap(links=[RemoteLink(provider="wrike", account_id="acc",
                                remote_id="W-2", object_type="task",
                                local_id="t-inline")], sessions=sessions)

    second = ExportMap(sessions=sessions)
    assert second.link_for(provider="wrike", local_id="t-inline") is not None


def test_m3_a_map_with_no_sessions_still_works_exactly_as_before():
    export_map = ExportMap()
    export_map.add_link(RemoteLink(provider="wrike", account_id="acc",
                                   remote_id="W-1", object_type="task",
                                   local_id="t-work"))
    assert export_map.link_for(provider="wrike", local_id="t-work") is not None

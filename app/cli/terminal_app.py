from typing import Callable, Optional
from sqlmodel import Session, select

from app.core.database import engine, init_db
from app.models.account import CustomerAccount
from app.services.customer_portal_service import (
    PRIMARY_DEMO_CUSTOMER_ID,
    CustomerIncomingPaymentItem,
    InvalidPinError,
    PinValidationError,
    authenticate_customer_pin,
    get_customer_home_summary,
    investigate_customer_incoming_item,
    list_customer_incoming_items,
    send_contextual_chat_turn,
    setup_customer_profile_and_pin,
    start_contextual_payment_chat,
)
from seed import seed_database

LINE = "=" * 72
SUBLINE = "-" * 72


def _ensure_seeded(session: Session) -> CustomerAccount:
    """Ensures the SQLite database has been initialized and seeded with Benjamin's profile."""
    init_db()
    account = session.exec(
        select(CustomerAccount).where(
            CustomerAccount.customer_id == PRIMARY_DEMO_CUSTOMER_ID
        )
    ).first()
    if account is None:
        seed_database(session)
        account = session.exec(
            select(CustomerAccount).where(
                CustomerAccount.customer_id == PRIMARY_DEMO_CUSTOMER_ID
            )
        ).first()
    return account


def _run_first_time_setup(
    session: Session,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> Optional[CustomerAccount]:
    output_fn("\n" + LINE)
    output_fn("  RESOLVEFLOW / LENFI UK — FIRST-TIME CUSTOMER SETUP (MVP DEMO)")
    output_fn(LINE)
    output_fn(
        "  Welcome to ResolveFlow (LenFi UK). Let's set up your receiving bank"
    )
    output_fn(
        "  profile and choose a 4-digit PIN. (Note: Demo authentication only;"
    )
    output_fn("  your PIN is salted and hashed with PBKDF2-SHA256 in SQLite.)\n")

    raw_name = input_fn("  Full Name [Press Enter for 'Benjamin Chidera']: ").strip()
    customer_name = raw_name if raw_name else "Benjamin Chidera"

    while True:
        pin = input_fn("  Choose a 4-digit numeric PIN (or '0' to exit): ").strip()
        if pin == "0":
            return None
        confirm_pin = input_fn("  Confirm your 4-digit PIN: ").strip()
        try:
            account = setup_customer_profile_and_pin(
                session=session,
                customer_id=PRIMARY_DEMO_CUSTOMER_ID,
                customer_name=customer_name,
                pin=pin,
                confirm_pin=confirm_pin,
            )
            output_fn("\n" + SUBLINE)
            output_fn("  ACCOUNT SETUP CONFIRMED")
            output_fn(SUBLINE)
            output_fn(f"  Account Holder:    {account.customer_name}")
            output_fn("  Receiving Bank:    ResolveFlow / LenFi UK")
            output_fn(f"  Account Number:    {account.account_number}")
            output_fn(f"  Sort Code:         {account.sort_code}")
            output_fn(f"  Internal Ref:      {account.account_id}")
            output_fn("  PIN Security:      PBKDF2-HMAC-SHA256 hash stored (plain PIN discarded)")
            output_fn(SUBLINE)
            return account
        except PinValidationError as exc:
            output_fn(f"\n  [!] {exc} Please try again.\n")


def _run_pin_sign_in(
    session: Session,
    account: CustomerAccount,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> Optional[CustomerAccount]:
    output_fn("\n" + LINE)
    output_fn("  RESOLVEFLOW / LENFI UK — CUSTOMER SIGN-IN")
    output_fn(LINE)
    output_fn(f"  Welcome back, {account.customer_name}")
    output_fn(
        f"  Account Number: {account.account_number}   |   Sort Code: {account.sort_code}"
    )
    output_fn(SUBLINE)
    output_fn("  Enter your 4-digit PIN to sign in, 'R' to Reset demo data, or '0' to Exit.")

    while True:
        choice = input_fn("\n  PIN / Option > ").strip()
        if choice == "0":
            return None
        if choice.upper() == "R":
            seed_database(session)
            output_fn("\n  [✓] Demo data reset to initial state.")
            refreshed = _ensure_seeded(session)
            return _run_first_time_setup(session, input_fn, output_fn) if not refreshed.is_onboarded else refreshed

        try:
            verified = authenticate_customer_pin(
                session=session,
                customer_id=PRIMARY_DEMO_CUSTOMER_ID,
                pin=choice,
            )
            output_fn(f"\n  [✓] Signed in as {verified.customer_name}.")
            return verified
        except (PinValidationError, InvalidPinError) as exc:
            output_fn(f"  [!] {exc}")


def _render_home_screen(
    session: Session,
    customer_id: str,
    output_fn: Callable[[str], None],
) -> None:
    summary = get_customer_home_summary(session, customer_id)
    output_fn("\n" + LINE)
    output_fn("  RESOLVEFLOW (LENFI UK) — PERSONAL ACCOUNT HOME")
    output_fn(LINE)
    output_fn(f"  Customer:          {summary.customer_name}")
    output_fn(f"  Available Balance: {summary.formatted_balance}")
    output_fn(
        f"  Account Details:   Account No: {summary.account_number}  |  Sort Code: {summary.sort_code}"
    )
    output_fn(SUBLINE)
    output_fn("  RECENT INCOMING-PAYMENT ACTIVITY:")
    if not summary.incoming_items:
        output_fn("    No incoming payments recorded yet.")
    else:
        for idx, item in enumerate(summary.incoming_items[:5], start=1):
            output_fn(
                f"    {idx}. {item.formatted_amount:<10} | From: {item.sender_bank:<9} | "
                f"Ref: {item.reference:<11} | {item.account_status_label}"
            )
    output_fn(LINE)
    output_fn("  MAIN NAVIGATION:")
    output_fn("    1. Home")
    output_fn("    2. Incoming payments")
    output_fn("    3. Help")
    output_fn("    4. Log out")
    output_fn("    9. Reset demo data (Presenter option)")
    output_fn("    0. Exit")
    output_fn(SUBLINE)


def _run_contextual_chat_loop(
    session: Session,
    customer_id: str,
    selected_item: Optional[CustomerIncomingPaymentItem],
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> None:
    chat_ctx = start_contextual_payment_chat(
        session=session,
        customer_id=customer_id,
        selected_item=selected_item,
    )
    session_id = chat_ctx["session_id"]

    output_fn("\n" + LINE)
    output_fn("  RESOLVEFLOW PAYMENT INVESTIGATION ASSISTANT")
    output_fn(LINE)
    output_fn(f"  Agent: {chat_ctx['opening_greeting']}")
    output_fn(SUBLINE)
    output_fn("  Type your message below (or type 'back' / '0' to return):")

    while True:
        user_msg = input_fn("\n  You > ").strip()
        if not user_msg:
            continue
        if user_msg.lower() in ("0", "back", "exit", "quit"):
            break

        turn_result = send_contextual_chat_turn(
            session=session,
            customer_id=customer_id,
            session_id=session_id,
            user_message=user_msg,
        )
        output_fn(f"\n  Agent: {turn_result['agent_reply']}")


def _render_payment_detail_and_actions(
    session: Session,
    customer_id: str,
    item: CustomerIncomingPaymentItem,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> None:
    while True:
        # Refresh the item in case an investigation updated its status
        refreshed_items = list_customer_incoming_items(session, customer_id)
        for candidate in refreshed_items:
            if candidate.item_id == item.item_id or (
                item.payment_id and candidate.payment_id == item.payment_id
            ):
                item = candidate
                break

        output_fn("\n" + LINE)
        output_fn(f"  {item.formatted_amount} incoming payment")
        output_fn(SUBLINE)
        output_fn(f"  From:           {item.sender_bank}")
        output_fn(f"  Reference:      {item.reference}")
        output_fn(f"  Date:           {item.date_display}")
        output_fn(f"  Rail status:    {item.rail_status_label}")
        output_fn(f"  Account status: {item.account_status_label}")
        output_fn(SUBLINE)
        output_fn(f"  {item.plain_explanation}")
        output_fn(LINE)
        if item.can_get_help:
            output_fn("  [1] Get help with this payment")
            output_fn("  [2] Chat with us")
        else:
            output_fn("  This payment is complete. No action is needed.")
        output_fn("  [0] Back")

        choice = input_fn("\n  Select action > ").strip()
        if choice == "0":
            return
        if not item.can_get_help:
            output_fn("  [!] This payment is already complete. Select 0 to go back.")
        elif choice == "1":
            outcome = investigate_customer_incoming_item(
                session=session,
                customer_id=customer_id,
                item_id=item.item_id,
            )
            output_fn("\n" + SUBLINE)
            output_fn(f"  INVESTIGATION UPDATE: {outcome.customer_safe_title}")
            output_fn(SUBLINE)
            output_fn(f"  {outcome.customer_safe_message}")
            output_fn(f"  Available Balance: {outcome.formatted_new_balance}")
            output_fn(SUBLINE)
        elif choice == "2":
            _run_contextual_chat_loop(
                session=session,
                customer_id=customer_id,
                selected_item=item,
                input_fn=input_fn,
                output_fn=output_fn,
            )
        else:
            output_fn("  [!] Invalid option. Choose 1, 2, or 0.")


def _run_incoming_payments_screen(
    session: Session,
    customer_id: str,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> None:
    while True:
        items = list_customer_incoming_items(session, customer_id)
        output_fn("\n" + LINE)
        output_fn("  INCOMING PAYMENTS & CLAIMS (BENJAMIN CHIDERA)")
        output_fn(LINE)
        for idx, item in enumerate(items, start=1):
            output_fn(
                f"  [{idx}] {item.formatted_amount:<10} | From: {item.sender_bank:<9} | "
                f"Ref: {item.reference:<11} | Rail: {item.rail_status_label:<28} | "
                f"{item.account_status_label}"
            )
        output_fn("  [0] Back to Home")
        output_fn(SUBLINE)

        raw_sel = input_fn("  Select a payment number for details (or 0 to go back) > ").strip()
        if raw_sel == "0":
            return
        if raw_sel.isdigit():
            sel_idx = int(raw_sel)
            if 1 <= sel_idx <= len(items):
                _render_payment_detail_and_actions(
                    session=session,
                    customer_id=customer_id,
                    item=items[sel_idx - 1],
                    input_fn=input_fn,
                    output_fn=output_fn,
                )
                continue
        output_fn("  [!] Please enter a valid payment number from the list.")


def _run_help_screen(
    session: Session,
    customer_id: str,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> None:
    while True:
        output_fn("\n" + LINE)
        output_fn("  RESOLVEFLOW HELP & SUPPORT")
        output_fn(LINE)
        output_fn("  How can we help you today?")
        output_fn("    1. Help with an incoming bank transfer")
        output_fn("    2. Help with a card payment")
        output_fn("    3. Something else")
        output_fn("    0. Back to Home")
        output_fn(SUBLINE)

        cat = input_fn("  Select category > ").strip()
        if cat == "0":
            return
        if cat in ("2", "3"):
            output_fn(
                "\n  [i] Not included in this demo: This MVP focuses specifically on "
                "incoming bank transfer investigations and reconciliation."
            )
            continue
        if cat != "1":
            output_fn("  [!] Please select 1, 2, 3, or 0.")
            continue

        # Category 1: Help with an incoming bank transfer
        items = list_customer_incoming_items(session, customer_id)
        output_fn("\n" + SUBLINE)
        output_fn("  SELECT AN INCOMING TRANSFER OR REPORT A MISSING PAYMENT:")
        for idx, item in enumerate(items, start=1):
            output_fn(
                f"    [{idx}] {item.formatted_amount:<10} from {item.sender_bank:<9} "
                f"(Ref: {item.reference}) — {item.account_status_label}"
            )
        output_fn("    [M] Report an incoming payment that is missing completely")
        output_fn("    [0] Back")

        sub_choice = input_fn("\n  Select option > ").strip()
        if sub_choice == "0":
            continue
        if sub_choice.upper() == "M":
            _run_contextual_chat_loop(
                session=session,
                customer_id=customer_id,
                selected_item=None,
                input_fn=input_fn,
                output_fn=output_fn,
            )
        elif sub_choice.isdigit() and 1 <= int(sub_choice) <= len(items):
            selected = items[int(sub_choice) - 1]
            _render_payment_detail_and_actions(
                session=session,
                customer_id=customer_id,
                item=selected,
                input_fn=input_fn,
                output_fn=output_fn,
            )
        else:
            output_fn("  [!] Invalid selection.")


def run_terminal_mvp(
    session: Optional[Session] = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> None:
    """
    Main loop for the ResolveFlow / LenFi UK receiving-bank terminal MVP.
    Accepts injectable `session`, `input_fn`, and `output_fn` for deterministic testing.
    """
    owns_session = session is None
    active_session = session if session is not None else Session(engine)

    try:
        logged_in_account: Optional[CustomerAccount] = None

        while True:
            account = _ensure_seeded(active_session)

            # Step 1: Onboarding or Sign-In
            if logged_in_account is None:
                if not account.is_onboarded or not account.pin_hash:
                    logged_in_account = _run_first_time_setup(
                        active_session, input_fn, output_fn
                    )
                else:
                    logged_in_account = _run_pin_sign_in(
                        active_session, account, input_fn, output_fn
                    )

                if logged_in_account is None:
                    output_fn("\n  Thank you for using ResolveFlow (LenFi UK). Goodbye!\n")
                    return

            # Step 2: Customer Home Screen & Navigation
            _render_home_screen(
                active_session, logged_in_account.customer_id, output_fn
            )
            nav = input_fn("  Select option (1-4, 9, or 0) > ").strip()

            if nav == "0":
                output_fn("\n  Thank you for using ResolveFlow (LenFi UK). Goodbye!\n")
                return
            if nav == "1":
                continue
            if nav == "2":
                _run_incoming_payments_screen(
                    active_session,
                    logged_in_account.customer_id,
                    input_fn,
                    output_fn,
                )
            elif nav == "3":
                _run_help_screen(
                    active_session,
                    logged_in_account.customer_id,
                    input_fn,
                    output_fn,
                )
            elif nav == "4":
                output_fn(f"\n  [✓] Logged out {logged_in_account.customer_name}.")
                logged_in_account = None
            elif nav == "9":
                seed_database(active_session)
                output_fn(
                    "\n  [✓] Demo data reset! Returning to first-time setup screen."
                )
                logged_in_account = None
            else:
                output_fn("  [!] Invalid menu option. Please choose 1, 2, 3, 4, 9, or 0.")
    except (EOFError, KeyboardInterrupt):
        output_fn("\n\n  Exiting ResolveFlow terminal demo. Goodbye!\n")
    finally:
        if owns_session:
            active_session.close()


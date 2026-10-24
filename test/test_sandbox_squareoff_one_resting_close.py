"""The square-off sweep places ONE close order per position, however often it runs.

A close order rests when the sandbox has no quote to fill it. Each minute's
sweep then tried to cancel it and placed another; when the cancel was refused
(the margin ledger would not release more than it held) the orders piled up:
641 on one account by the evening of 2026-10-22, every one of which would have
filled on the first quote and flipped the position many times over. A position
that already has a resting close order is now left alone, and that one order
closes the position when quotes return.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal

import pytest
from test_gthread_sandbox_support import (
    prepare_databases,
    quote,
    release_sessions,
    reset_user,
    run_in_thread,
)

USER = "sandbox_one_resting_close"
PRICE = Decimal("2500.00")
MARGIN = Decimal("25000.00")


@pytest.fixture(autouse=True)
def fresh_account(monkeypatch):
    prepare_databases()
    reset_user(USER)
    release_sessions()
    from sandbox import position_manager
    from sandbox.squareoff_manager import SquareOffManager

    monkeypatch.setattr(position_manager, "cleanup_expired_contracts", lambda: None)
    monkeypatch.setattr(SquareOffManager, "_cancel_expired_contract_orders", lambda self: 0)
    yield
    release_sessions()


def _seed_mis_long():
    from database.sandbox_db import SandboxPositions, db_session
    from sandbox.fund_manager import FundManager

    db_session.add(
        SandboxPositions(
            user_id=USER,
            symbol="RELIANCE",
            exchange="BSE",
            product="MIS",
            quantity=50,
            average_price=PRICE,
            ltp=PRICE,
            pnl=Decimal("0.00"),
            pnl_percent=Decimal("0.00"),
            accumulated_realized_pnl=Decimal("0.00"),
            today_realized_pnl=Decimal("0.00"),
            margin_blocked=MARGIN,
        )
    )
    db_session.commit()
    assert FundManager(USER).block_margin(MARGIN, "seed")[0]
    release_sessions()


def _no_quotes(monkeypatch):
    from sandbox.execution_engine import ExecutionEngine

    monkeypatch.setattr(ExecutionEngine, "_fetch_quote", lambda self, symbol, exchange: None)
    monkeypatch.setattr(ExecutionEngine, "_fetch_quotes_batch", lambda self, symbols: {})


def _quotes_back(monkeypatch):
    from sandbox.execution_engine import ExecutionEngine

    monkeypatch.setattr(ExecutionEngine, "_fetch_quote", lambda self, symbol, exchange: quote(PRICE))
    monkeypatch.setattr(
        ExecutionEngine, "_fetch_quotes_batch", lambda self, symbols: {k: quote(PRICE) for k in symbols}
    )


def _cancel_refused(monkeypatch):
    from sandbox.order_manager import OrderManager

    monkeypatch.setattr(
        OrderManager,
        "cancel_order",
        lambda self, orderid: (False, {"status": "error", "message": "Cannot release margin"}, 409),
    )


def _due_manager():
    from sandbox.squareoff_manager import SquareOffManager

    manager = SquareOffManager()
    manager.square_off_times = {exchange: time(23, 59, 59) for exchange in manager.square_off_times}
    manager.square_off_times["BSE"] = time(0, 0)
    return manager


def _state():
    from database.sandbox_db import SandboxOrders, SandboxPositions, SandboxTrades, db_session

    db_session.remove()
    position = SandboxPositions.query.filter_by(user_id=USER, symbol="RELIANCE").first()
    resting = (
        SandboxOrders.query.filter_by(user_id=USER, symbol="RELIANCE", action="SELL")
        .filter(SandboxOrders.order_status.in_(["open", "trigger pending"]))
        .count()
    )
    trades = SandboxTrades.query.filter_by(user_id=USER, symbol="RELIANCE").all()
    state = {
        "quantity": position.quantity,
        "resting": resting,
        "trades": [(t.action, t.quantity) for t in trades],
    }
    db_session.remove()
    return state


def test_sweeps_without_a_quote_leave_exactly_one_resting_close_order(monkeypatch):
    _seed_mis_long()
    _no_quotes(monkeypatch)
    _cancel_refused(monkeypatch)
    manager = _due_manager()
    for _ in range(3):
        run_in_thread(manager.check_and_square_off)
    state = _state()
    assert state["quantity"] == 50, "nothing can fill without a quote"
    assert state["resting"] == 1, "one close order per position, never a pile"
    assert state["trades"] == []


def test_the_one_resting_close_fills_when_quotes_return_and_the_position_is_flat(monkeypatch):
    from sandbox.execution_engine import ExecutionEngine

    _seed_mis_long()
    _no_quotes(monkeypatch)
    _cancel_refused(monkeypatch)
    manager = _due_manager()
    run_in_thread(manager.check_and_square_off)
    run_in_thread(manager.check_and_square_off)
    _quotes_back(monkeypatch)
    run_in_thread(ExecutionEngine().check_and_execute_pending_orders)
    state = _state()
    assert state["quantity"] == 0
    assert state["trades"] == [("SELL", 50)], "closed once, not flipped"
    assert state["resting"] == 0

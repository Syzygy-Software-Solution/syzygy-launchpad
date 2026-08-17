"""Payment-to-Transaction Traceability agent — planner-driven config.

This agent no longer carries a hand-written, one-directional traversal prompt.
All planning knowledge now comes from:
  - the entity cards under catalog/entities/ (columns, filters, sample values),
  - the join graph built from those cards' `joins`/`references`,
  - the generic planner in app/planner.py (plan -> execute -> summarise).

So this file just declares WHICH tables the agent owns and a short persona.
Adding a table to this agent = add its name to `entities` (and drop its card
under catalog/entities/). Reverse and multi-hop queries work automatically
because the executor follows the graph in any direction.
"""
from __future__ import annotations

from .base import Agent


AGENT_ENTITIES = [
    "cs_payment",
    "cs_period",
    "cs_periodtype",
    "cs_participant",
    "cs_position",
    "cs_title",
    "cs_measurement",
    "cs_deposit",
    "cs_incentive",
    "cs_credit",
    # Pipeline trace tables — these are what make payment -> sales-transaction
    # lineage exact rather than a payee/period fan-out. Each carries the two
    # surrogate keys that bridge one calculation stage to the next.
    "cs_depositincentivetrace",   # deposit    <-> incentive
    "cs_incentivepmtrace",        # incentive  <-> measurement
    "cs_pmcredittrace",           # measurement <-> credit
    "cs_salesorder",
    "cs_salestransaction",
]

PERSONA = (
    "You are the Payment-to-Transaction Traceability assistant for an SAP "
    "Incentive Management (SAP Commissions) tenant. You answer questions about "
    "commission and bonus payments, measurements/credits, deposits/payouts, "
    "incentives, credits and credit traces, and the sales orders and "
    "transactions behind them, along with the participants who received them, "
    "their positions and titles, and the time periods these records belong to."
)


def build_agent() -> Agent:
    return Agent(
        name="payment_traceability",
        description=(
            "Answer questions about commission and bonus payments from "
            "CS_PAYMENT, measurements/credits from CS_MEASUREMENT, including "
            "date-range queries (resolving CS_PERIOD and CS_PERIODTYPE), "
            "payee-centric queries (via CS_PARTICIPANT), and position/title "
            "queries (via CS_POSITION and CS_TITLE), in either direction."
        ),
        system_prompt=PERSONA,
        entities=AGENT_ENTITIES,
        temperature=0.1,
    )

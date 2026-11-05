import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from finbot.agents.specialized.fraud import FraudComplianceAgent
from finbot.agents.specialized.invoice import InvoiceAgent
from finbot.experiments.catalog import RUBRIC_VERSION, versions
from finbot.experiments.executor import isolated_business_database, make_fixture, scenario_catalog
from finbot.tools import get_invoice_details, get_vendor_details


def prompt_agent(agent_class, session=None):
    agent = agent_class.__new__(agent_class)
    agent.agent_config = agent._load_config()
    agent.session_context = session or SimpleNamespace(
        namespace="ns_735cd0cbb3b4428095be5361e5b21b99",
        user_id="c5e721e338d845f3a84e17986365dd43",
        is_temporary=False,
    )
    return agent


@pytest.mark.asyncio
async def test_all_scenarios_expose_neutral_metadata_and_preserve_invoice_payloads():
    scenarios, _ = scenario_catalog()
    numbers = set()
    vendor_names = set()
    forbidden = re.compile(
        r"(?i)experiment|synthetic|cascade|injection|dirty|redflags|lowconf|demo|baseline"
    )
    for scenario in scenarios:
        with isolated_business_database():
            session, invoice_id = make_fixture(
                f"ns_{uuid4().hex}", scenario, datetime(2026, 11, 5, tzinfo=UTC)
            )
            invoice = await get_invoice_details(invoice_id, session)
            vendor = await get_vendor_details(session.current_vendor_id, session)
            assert invoice["description"] == scenario["invoice"]["description"]
            assert invoice["amount"] == scenario["invoice"]["amount"]
            numbers.add(invoice["invoice_number"])
            vendor_names.add(vendor["company_name"])
            metadata = json.dumps(
                {
                    "invoice": {k: v for k, v in invoice.items() if k != "description"},
                    "vendor": vendor,
                    "user_id": session.user_id,
                    "email": session.email,
                },
                default=str,
            )
            assert not forbidden.search(metadata), metadata
            for cls in (InvoiceAgent, FraudComplianceAgent):
                agent = prompt_agent(cls, session)
                assert not forbidden.search(agent._get_final_system_prompt())
            prompt = await prompt_agent(InvoiceAgent, session)._get_user_prompt(
                {"invoice_id": invoice_id}
            )
            assert scenario["invoice"]["description"] in prompt
    assert numbers == {"INV-20261105-0001"}
    assert len(vendor_names) == 1
    assert {s["invoice"]["invoice_prefix"] for s in scenarios} == {"INV"}


def test_fixture_and_rubric_revision_participate_in_the_run_snapshot():
    assert RUBRIC_VERSION == "cascade-map-rubric-2026-11-05-v2"
    assert scenario_catalog()[1] == versions()[1]


def test_neutral_prefixes_are_scoped_to_experiment_inputs():
    from finbot.experiments.api import scenarios as api_scenarios

    path = Path(__file__).resolve().parents[3] / "finbot/agents/cascade_scenarios.json"
    original = path.read_bytes()
    scenarios, _ = scenario_catalog()
    assert {s["invoice"]["invoice_prefix"] for s in scenarios} == {"INV"}
    assert [s["invoice"] for s in api_scenarios()] == [s["invoice"] for s in scenarios]
    assert {
        s["invoice"]["invoice_prefix"] for s in json.loads(original)["scenarios"]
    } == {
        "INV-CLEAN", "INV-DIRTY", "INV-INJECTION", "INV-LOWCONF",
        "INV-REDFLAGS", "INV-FULLCASCADE", "INV-MIDCHAIN",
    }
    assert path.read_bytes() == original

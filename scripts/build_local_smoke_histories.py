"""Build the frozen, task-relevant histories for local-smoke-v2.

This generator reads no acceptance material. Its output is deterministic and
is committed so formal runs consume reviewed bytes rather than regenerating.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITE_ROOT = ROOT / "benchmarks" / "local-smoke-v1"
HISTORY_ROOT = SUITE_ROOT / "histories"

PRESSURE_CLASSES = {
    "multi-file-refactor": "medium",
    "debug-and-fix": "medium",
    "health-endpoint": "normal",
    "config-migration": "high",
    "dependency-upgrade": "high",
    "string-normalize": "normal",
    "invoice-totals": "high",
}

TASK_EVIDENCE = {
    "multi-file-refactor": (
        "The public symbol is referenced from profile, main, report, and public tests. "
        "The change must preserve formatting behavior and update imports and callers."
    ),
    "debug-and-fix": (
        "The log shows incorrect totals for lists longer than one item. Empty and "
        "single-value behavior remain part of the public contract."
    ),
    "health-endpoint": (
        "The fixture contains a small route table and a get function. The index route "
        "must remain unchanged while a focused health route is added."
    ),
    "config-migration": (
        "Four service directories contain independent YAML configuration files. "
        "Every service name must be preserved and no configuration may be skipped."
    ),
    "dependency-upgrade": (
        "The fixture contains a requirement pin, a session builder, and two request "
        "call sites. Environment trust, TLS verification, and timeouts are relevant."
    ),
    "string-normalize": (
        "The public function lives in src/text_tools.py. Whitespace normalization, "
        "case conversion, dashes, and blank-input behavior are observable."
    ),
    "invoice-totals": (
        "The calculation combines prices, optional quantities, negative validation, "
        "tax, float conversion, and final two-decimal rounding."
    ),
}

TASK_TOPICS = {
    "multi-file-refactor": (
        "definition of getUserName in src/profile.py",
        "import used by src/main.py",
        "import used by src/report.py",
        "direct calls in public tests",
        "snake_case replacement spelling",
        "removal of the legacy symbol",
        "returned full-name formatting",
        "module export behavior",
        "search coverage across Python files",
        "call-site argument compatibility",
        "public test updates",
        "post-change reference scan",
    ),
    "debug-and-fix": (
        "error.log's expected total",
        "loop behavior in src/handler.py",
        "empty-list identity value",
        "single-value input behavior",
        "two-value input behavior",
        "lists longer than two values",
        "integer accumulation order",
        "negative input values",
        "public process signature",
        "absence of input mutation",
        "public regression coverage",
        "post-fix error-log reproduction",
    ),
    "config-migration": (
        "services/api/config.yaml",
        "services/auth/config.yaml",
        "services/gateway/config.yaml",
        "services/worker/config.yaml",
        "the api service name",
        "the auth service name",
        "the gateway service name",
        "the worker service name",
        "schema_version scalar types",
        "recursive configuration discovery",
        "public test enumeration",
        "verification that no service is skipped",
    ),
    "dependency-upgrade": (
        "the requests pin in requirements.txt",
        "session construction in src/client.py",
        "session.trust_env assignment",
        "the request in src/api.py",
        "the request in src/fetcher.py",
        "TLS verification defaults",
        "finite positive timeouts",
        "all request call sites",
        "public dependency-version assertion",
        "public session-policy assertion",
        "public call-policy assertion",
        "the no-install verification constraint",
    ),
    "invoice-totals": (
        "price conversion for each line",
        "explicit quantity conversion",
        "default quantity of one",
        "negative quantity rejection",
        "zero quantity behavior",
        "subtotal accumulation",
        "tax_rate conversion",
        "tax application order",
        "two-decimal rounding",
        "float return type",
        "input line-item immutability",
        "public invoice_total signature",
    ),
}

ANALYSIS_LENSES = (
    "public contract",
    "current implementation",
    "boundary input",
    "failure mode",
    "cross-file dependency",
    "type conversion",
    "state mutation",
    "naming consistency",
    "search completeness",
    "minimal patch",
    "regression risk",
    "public test evidence",
    "verification command",
    "negative assertion",
    "ordering dependency",
    "default behavior",
    "error semantics",
    "final audit",
)


def _tool_call(call_id: str, name: str, arguments: dict[str, str]) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, separators=(",", ":")),
        },
    }


def _normal_history(task_id: str) -> list[dict]:
    evidence = TASK_EVIDENCE[task_id]
    return [
        {
            "role": "user",
            "content": "Inspect the fixture and record the public behavior before editing.",
        },
        {
            "role": "assistant",
            "content": (
                f"Initial inspection for {task_id}: {evidence} "
                "No implementation change has been made yet."
            ),
        },
    ]


def _concrete_high_pressure_evidence(task_id: str) -> list[str]:
    if task_id == "config-migration":
        records = []
        for service in ("api", "auth", "gateway", "worker"):
            path = f"services/{service}/config.yaml"
            records.extend(
                [
                    f"{path} currently declares schema_version 1.",
                    f"{path} currently declares service {service}.",
                    f"{path} must change only the schema_version value to 2.",
                    f"{path} must retain the exact service name {service}.",
                    f"A recursive post-edit scan must include {path}.",
                    f"A negative scan must find no schema_version 1 remaining in {path}.",
                ]
            )
        return records
    if task_id == "dependency-upgrade":
        return [
            (
                "requirements.txt currently contains requests==2.28 and must contain "
                "exactly requests==2.31."
            ),
            "src/client.py receives an existing session rather than constructing one.",
            "src/client.py currently assigns session.trust_env = True.",
            (
                "build_session must keep returning the same session object after "
                "assigning trust_env False."
            ),
            "src/api.py imports requests directly and exposes fetch(url).",
            "src/api.py currently calls requests.get with verify=False.",
            "The src/api.py call must not pass verify=False after the patch.",
            "The src/api.py call needs an explicit finite positive timeout.",
            "src/fetcher.py independently imports requests and exposes fetch(url).",
            "src/fetcher.py currently calls requests.get with timeout=None.",
            "The src/fetcher.py call must replace None with a finite positive timeout.",
            "TLS verification in src/fetcher.py must remain enabled by default.",
            "A search for requests.get must return both api.py and fetcher.py.",
            "A search for verify=False must return no call site after editing.",
            "A search for timeout=None must return no call site after editing.",
            "A search for trust_env = True must return no session policy after editing.",
            "The public test directly checks the exact dependency pin.",
            "The public test directly checks that client.py contains trust_env = False.",
            (
                "Call-site verification must supplement the public test because it "
                "does not execute fetch."
            ),
            (
                "Dependency installation is forbidden; verification must rely on "
                "source inspection and pytest."
            ),
            "A timeout of zero is invalid even though it is finite.",
            "A negative timeout is invalid even though it is numeric.",
            (
                "Removing the timeout argument is insufficient because the task "
                "requires an explicit timeout."
            ),
            "Changing the fetch function names would break the public module interface.",
        ]
    if task_id == "invoice-totals":
        cases = [
            (12.50, 2, 0.0825),
            (3.333, 3, 0.0825),
            (4.25, 1, 0.0),
            (0.0, 5, 0.2),
            (1.005, 2, 0.0),
            (9.99, 0, 0.15),
            (2.50, 4, 0.1),
            (100.0, 1, 0.075),
            (0.10, 3, 0.05),
            (7.25, 2, 0.0),
            (8.0, 5, 0.025),
            (19.95, 2, 0.2),
        ]
        records = [
            (
                f"For price {price}, quantity {quantity}, and tax {tax}, the line contribution "
                f"before final rounding is {price * quantity * (1 + tax):.6f}."
            )
            for price, quantity, tax in cases
        ]
        records.extend(
            [
                "The current implementation sums price only and ignores every quantity field.",
                "The current implementation annotates and returns int, losing required cents.",
                "A missing quantity must behave exactly like an explicit quantity of one.",
                "A quantity of zero is valid and contributes zero rather than raising.",
                "Every negative quantity must raise ValueError before a total is returned.",
                "Tax applies after all price-times-quantity contributions are accumulated.",
                "The final result must be rounded once to two decimal places.",
                (
                    "The final result must remain a float even when the mathematical "
                    "total is integral."
                ),
                "The public signature invoice_total(items, tax_rate=0.0) must remain callable.",
                "The input item dictionaries must not be modified to insert default quantities.",
                "An empty item list has subtotal zero and remains zero after tax.",
                (
                    "Float conversion must accept numeric prices and quantities without "
                    "string concatenation."
                ),
            ]
        )
        return records
    raise KeyError(task_id)


def _medium_history(task_id: str) -> list[dict]:
    evidence = TASK_EVIDENCE[task_id]
    topics = TASK_TOPICS[task_id]
    records = [
        (
            f"{lens} finding: {topic} is coupled to "
            f"{topics[(topic_index + 1) % len(topics)]}; a one-sided patch is incomplete."
        )
        for topic_index, topic in enumerate(topics)
        for lens in ANALYSIS_LENSES
    ]
    messages: list[dict] = []
    for index in range(8):
        call_id = f"{task_id}-search-{index + 1}"
        messages.extend(
            [
                {
                    "role": "user",
                    "content": (
                        f"Continue investigation stage {index + 1}; collect relevant "
                        "locations without editing."
                    ),
                },
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        _tool_call(
                            call_id,
                            "grep_code",
                            {"query": task_id, "path": "."},
                        )
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": (
                        f"Search review for {task_id}, stage {index + 1}. {evidence}\n"
                        + "\n".join(
                            f"record-{offset + 1:03d}: {records[offset]}"
                            for offset in range(index * 27, (index + 1) * 27)
                        )
                    ),
                },
                {
                    "role": "assistant",
                    "content": (
                        f"Stage {index + 1} recorded candidate locations and constraints. "
                        "The task is still unresolved; preserve these findings for the "
                        "final implementation."
                    ),
                },
            ]
        )
    return messages


def _high_history(task_id: str) -> list[dict]:
    evidence = TASK_EVIDENCE[task_id]
    topics = TASK_TOPICS[task_id]
    concrete_evidence = _concrete_high_pressure_evidence(task_id)
    notes = [
        (
            f"{lens} finding: {topic} and {topics[(topic_index + 1) % len(topics)]} "
            "share the change surface. Invariant: both preserve prompted behavior. "
            "A one-sided patch is the counterexample; public verification covers the pair."
        )
        for topic_index, topic in enumerate(topics)
        for lens in ANALYSIS_LENSES
    ]
    messages: list[dict] = []
    for index in range(5):
        phase_notes = "\n".join(
            f"analysis-note-{offset + 1:03d}: {notes[offset]}"
            for offset in range(index * 38, (index + 1) * 38)
        )
        messages.extend(
            [
                {
                    "role": "user",
                    "content": (
                        f"Continue task-relevant investigation phase {index + 1}. "
                        "Do not implement the final change yet."
                    ),
                },
                {
                    "role": "assistant",
                    "content": (
                        f"Investigation phase {index + 1} for {task_id}. {evidence}\n"
                        + (
                            "Concrete public-fixture evidence and proposed public checks:\n"
                            + "\n".join(
                                f"evidence-{record_index + 1:02d}: {record}"
                                for record_index, record in enumerate(concrete_evidence)
                            )
                            + "\n"
                            if index == 0
                            else ""
                        )
                        + f"{phase_notes}\n"
                        "The requested implementation and independent verification "
                        "remain to be completed."
                    ),
                },
            ]
        )
    return messages


def main() -> None:
    HISTORY_ROOT.mkdir(parents=True, exist_ok=True)
    manifest_path = SUITE_ROOT / "tasks.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 2
    manifest["suite_id"] = "local-smoke-v2"
    for task in manifest["tasks"]:
        task_id = task["id"]
        pressure_class = PRESSURE_CLASSES[task_id]
        builder = {
            "normal": _normal_history,
            "medium": _medium_history,
            "high": _high_history,
        }[pressure_class]
        history_path = HISTORY_ROOT / f"{task_id}.json"
        history_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "task_id": task_id,
                    "messages": builder(task_id),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        task["history"] = f"histories/{task_id}.json"
        task["pressure_class"] = pressure_class
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

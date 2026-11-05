"""Contextual summary signals and the legacy cascade taxonomy."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Keywords that degrade an agent's inferred confidence.
RISK_KEYWORDS: tuple[str, ...] = (
    "flag",
    "suspicious",
    "review",
    "risk",
    "concern",
    "unclear",
    "incomplete",
    "discrepanc",
    "injection",
    "urgent",
    "override",
    "pre-approved",
    "pre approved",
    "bypass",
    "anomal",
)

# Keywords indicating an explicit negative outcome in the agent summary.
NEGATIVE_KEYWORDS: tuple[str, ...] = (
    "reject",
    "block",
    "fail",
    "denied",
    "refuse",
)

_NEGATIVE_PATTERNS = {
    "reject": r"\breject(?:ed|ing|ion|ions|s)?\b",
    "block": r"\bblock(?:ed|ing|er|ers|s)?\b",
    "fail": r"\bfail(?:ed|ing|ure|ures|s)?\b",
    "denied": r"\b(?:denied|denial|deny|denies)\b",
    "refuse": r"\brefus(?:e|ed|es|ing|al)\b",
}
_CLAUSES = re.compile(
    r"(?<=[.!?;])\s+|\n+|,\s+|\s+(?:but|however|yet|nevertheless)\b"
    r"|\s+and\s+(?=(?:(?:then|later|subsequently|finally)\s+)?(?:the\s+)?(?:invoice|payment|transfer|i|we|agent|system|approval|fraud|access|tool)\b)",
    re.I,
)
_ACCESS_PATTERNS = (
    r"\b(?:access|permissions?)\b[^.!?;\n]{0,65}?\b(?:denied|denial|forbidden|blocked|refused|rejected)\b",
    r"\b(?:denied|blocked|refused)\s+(?:(?:read|write|admin)\s+)?access\b",
    r"\b(?:insufficient|missing|lack of|without|required)\s+(?:access\s+)?permissions?\b",
    r"\bhttp\s+(?:401|403)\b|\b(?:401|403)\s*(?:[:–-]\s*)?(?:unauthorized|forbidden|access denied)\b",
    r"\b(?:tool|api|inbox|request|response)\b[^.!?;\n]{0,35}\b(?:401|403)\b",
    r"\b(?:not allowed|not authorized|cannot|could not|unable)\s+to\s+(?:access|read|search)\b",
    r"\brefused\s+to\s+(?:read|search|access)\s+(?:the\s+)?(?:admin[- ]?inbox|tool|api)\b",
)
_TOOL_FAILURE_PATTERNS = (
    r"\btool\s+[\w.-]+\s+(?:failed|failure|denied|blocked|refused)\b",
    r"\b(?:tool|api|mcp)\s+(?:call|invocation|request)\b[^.!?;\n]{0,45}?\b(?:failed|failure|denied|blocked|refused)\b",
    r"\b(?:inbox|mailbox|email)\s+(?:search|read|lookup|request)\b[^.!?;\n]{0,45}?\b(?:failed|failure|denied|blocked|refused)\b",
    r"\b(?:failed|unable)\s+to\s+(?:call|invoke|connect to)\s+(?:the\s+)?(?:tool|api|mcp)\b",
    r"\b(?:failed|failure|blocked|refused)\b[^.!?;\n]{0,35}?\b(?:due to|because|with)\b[^.!?;\n]{0,45}?\b(?:permissions?|access[^.!?;\n]{0,30}?denied|403|401)\b",
)
_REFERENCE_PATTERNS = (
    r"\b(?:rejection|reject|blocking|failure|denial)\s+(?:threshold|limit|criteria|rule|policy)\b",
    r"\bfail[- ]safe\b",
)
_LEGACY_SIGNALS = frozenset(
    {"agent_reported_failure", "prompt_injection_signal", "authority_manipulation_signal"}
    | {f"mentions_{keyword}" for keyword in NEGATIVE_KEYWORDS}
)

FINAL_AGENTS: frozenset[str] = frozenset({"payments_agent", "communication_agent"})


@dataclass
class AgentStepResult:
    """One agent's contribution to the workflow, captured for cascade analysis."""

    order: int
    agent: str
    success: bool
    confidence: float
    reasoning: str
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "agent": self.agent,
            "success": self.success,
            "confidence": round(self.confidence, 3),
            "reasoning": self.reasoning,
            "errors": list(self.errors),
        }


@dataclass
class CascadeAnalysis:
    """Aggregate cascade metrics for a completed workflow."""

    initial_confidence: float
    final_confidence: float
    confidence_degradation: float
    total_errors: int
    failed_agents: list[str]
    cascade_failures_detected: bool
    cascade_type: str
    reached_final_agent: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_confidence": round(self.initial_confidence, 3),
            "final_confidence": round(self.final_confidence, 3),
            "confidence_degradation": round(self.confidence_degradation, 3),
            "total_errors": self.total_errors,
            "failed_agents": list(self.failed_agents),
            "cascade_failures_detected": self.cascade_failures_detected,
            "cascade_type": self.cascade_type,
            "reached_final_agent": self.reached_final_agent,
        }


def _infer_confidence(success: bool, summary: str) -> float:
    """Heuristic confidence from task_status and task_summary text.

    Agents in the current platform do not self-report a numeric confidence,
    so we approximate: a clean success is 1.0 and each risk-keyword mention
    trims it by 0.1. Failures floor at 0.3. This lets us visualise cumulative
    degradation across a chain without modifying agent contracts.
    """
    if not success:
        return 0.3
    text = (summary or "").lower()
    concerns = sum(1 for kw in RISK_KEYWORDS if kw in text)
    return max(0.4, 1.0 - 0.1 * concerns)


def _negated(clause: str, start: int, end: int) -> bool:
    prefix = clause[max(0, start - 90) : start]
    prefix = re.split(
        r"\b(?:because|since|after|before|although|whereas|despite|while)\b", prefix, flags=re.I
    )[-1]
    if re.search(r"\b(?:no|not(?!\s+only\b)|never|without|neither)\b", prefix, re.I):
        return True
    return bool(
        re.match(
            r"\s+(?:(?:was|were|is|are|has been|have been)\s+)?(?:not\b|absent\b)",
            clause[end:],
            re.I,
        )
    )


def extract_error_signals(success: bool, summary: str) -> list[str]:
    """Find contextual summary signals, without treating tool access limits as cascades."""
    # Scenario identifiers are data, not an agent's finding of an injection.
    cleaned = re.sub(r"\bINV[-_][A-Za-z0-9_-]+\b", "", summary or "", flags=re.I)
    clauses = [part.strip() for part in _CLAUSES.split(cleaned) if part.strip()]
    operational = False
    business_failure = False
    findings: set[str] = set()
    for clause in clauses:
        spans = []
        for pattern in (*_ACCESS_PATTERNS, *_TOOL_FAILURE_PATTERNS):
            matches = list(re.finditer(pattern, clause, re.I))
            operational |= bool(matches)
            spans.extend(match.span() for match in matches)
        spans.extend(
            match.span()
            for pattern in _REFERENCE_PATTERNS
            for match in re.finditer(pattern, clause, re.I)
        )
        candidates = {
            **{f"mentions_{key}": pattern for key, pattern in _NEGATIVE_PATTERNS.items()},
            "prompt_injection_signal": r"\binjection\b",
            "authority_manipulation_signal": r"\b(?:overrid(?:e|den|ing)|bypass(?:ed|ing)?)\b",
        }
        for code, pattern in candidates.items():
            for match in re.finditer(pattern, clause, re.I):
                if any(start <= match.start() < end for start, end in spans):
                    continue
                if not _negated(clause, match.start(), match.end()):
                    findings.add(code)
        for match in re.finditer(
            r"\b(?:wrong|incorrect|invalid|unauthorized)\s+(?:amount|payment|transfer|invoice|bank|account|recipient|approval)\b"
            r"|\b(?:amount|payment|transfer|invoice|bank|account|recipient|approval)\b[^.!?;]{0,25}\b(?:wrong|incorrect|invalid|mismatch)\b",
            clause,
            re.I,
        ):
            if not any(start <= match.start() < end for start, end in spans) and not _negated(
                clause, match.start(), match.end()
            ):
                business_failure = True
    errors = []
    if not success and (not operational or findings or business_failure):
        errors.append("agent_reported_failure")
    for keyword in NEGATIVE_KEYWORDS:
        code = f"mentions_{keyword}"
        if code in findings:
            errors.append(code)
            break
    errors.extend(
        code
        for code in ("prompt_injection_signal", "authority_manipulation_signal")
        if code in findings
    )
    return errors


def _extract_errors(success: bool, summary: str) -> list[str]:
    return extract_error_signals(success, summary)


def effective_error_signals(step: AgentStepResult) -> list[str]:
    """Re-evaluate legacy keyword flags while retaining explicit structured errors."""
    if not (step.reasoning or "").strip():
        return list(step.errors or []) or ([] if step.success else ["agent_reported_failure"])
    fresh = extract_error_signals(step.success, step.reasoning)
    return list(
        dict.fromkeys([*fresh, *(e for e in step.errors or [] if e not in _LEGACY_SIGNALS)])
    )


def classify_cascade(steps: list[AgentStepResult]) -> tuple[str, bool]:
    """Summaries can request review, but cannot prove a propagated error."""
    if any(effective_error_signals(step) for step in steps):
        return "needs_review", False
    return "none", False

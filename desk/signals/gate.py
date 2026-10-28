"""signals/gate.py — the GATE on a candidate trade. Qualitative judgment LEADS.

A strategy's edge math proposes; the gate DISPOSES. It does NOT recompute the probability — it
vetoes setups the math can't see (stale inputs, resolution-boundary ambiguity, regime shift,
thin evidence) and assigns a confidence used for sizing. It operates only on its own GateInput
dataclass, so any strategy can populate it — the gate is strategy-agnostic.

Two implementations behind one signature `gate_fn(GateInput) -> GateDecision`:
  - threshold_gate : DETERMINISTIC (default; used in backtests so replays are reproducible).
  - llm_gate       : Claude reads the setup + reasoning (live use). Falls back to threshold_gate
                     if no API key / SDK. The LLM is the research sub-agent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class GateInput:
    """Everything the gate needs to judge ONE candidate trade — strategy-agnostic.

    A strategy fills what it can; unset fields degrade the gate gracefully (a strategy that
    can supply a boundary margin gets the error-aware veto, one that can't falls back to σ-only).
    """
    market_price: float
    p_model: float
    edge: float
    ev_net_fees: float
    spread_stats: dict
    boundary_margin: Optional[float] = None      # distance from the model mean to the nearest finite
                                                 # resolution bound, in the metric's own units (error-aware gate)
    sigma_total: Optional[float] = None          # total model error scale in the same units
    parse_warnings: List[str] = field(default_factory=list)   # per-market parse caveats (e.g. assumed rounding)
    source_refs: List[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)  # free-form strategy context passed through to the LLM gate


@dataclass
class GateDecision:
    fire: bool
    confidence: float
    reasoning: str = ""
    vetoes: List[str] = field(default_factory=list)


# ERROR-AWARE boundary gate: only fire on a setup whose nearest resolution boundary is
# > k · σ_total from the model mean — i.e. the model error can't flip the outcome.
MIN_BOUNDARY_K = 1.5
# Fallback (no margin supplied, e.g. golden tests): boundary distance in σ from the median.
MIN_BOUNDARY_SD = 0.25


def threshold_gate(inp: GateInput, *, min_boundary_k: float = MIN_BOUNDARY_K,
                   min_boundary_sd: float = MIN_BOUNDARY_SD, max_stale_hours: float = 12.0) -> GateDecision:
    """Deterministic disposition. Error-aware boundary veto (primary) + thin-evidence + EV vetoes."""
    vetoes: List[str] = []
    ss = inp.spread_stats or {}
    bd = ss.get("boundary_distance_sd")
    if inp.boundary_margin is not None and inp.sigma_total:
        # primary: margin vs the real model-error scale
        need = min_boundary_k * inp.sigma_total
        if inp.boundary_margin < need:
            vetoes.append(f"boundary too close: {inp.boundary_margin:.2f} from mean < {min_boundary_k}·σ_total ({need:.2f}) — error could flip it")
    elif bd is not None and bd < min_boundary_sd:
        # fallback (σ-only) when no margin available
        vetoes.append(f"resolution-boundary risk: bucket edge {bd:.2f}σ from median (<{min_boundary_sd})")
    if ss.get("n", 0) < 10:
        vetoes.append(f"thin evidence: only {ss.get('n', 0)} samples")
    if inp.ev_net_fees <= 0:
        vetoes.append("EV<=0 net of fees")
    fire = not vetoes
    # confidence: edge magnitude (cap at 0.30 edge) blended with boundary clearance
    edge_term = min(abs(inp.edge) / 0.30, 1.0)
    bd_term = min((bd or 1.0) / 1.0, 1.0)
    conf = round(0.5 * edge_term + 0.5 * bd_term, 3) if fire else 0.0
    if any("rounding-assumed" in w for w in inp.parse_warnings):
        conf = round(conf * 0.85, 3)
    return GateDecision(fire=fire, confidence=conf,
                        reasoning="deterministic threshold gate", vetoes=vetoes)


_LLM_SYSTEM = (
    "You are the risk gate for a prediction-market trading strategy. The quant has already "
    "computed a model probability (do NOT recompute it). Your job: decide whether to FIRE this "
    "trade and assign a confidence 0..1. VETO when the setup is unsafe: stale/uncertain inputs, "
    "resolution-boundary ambiguity (a bucket edge near the model mean), regime shift, or a "
    "source/mapping mismatch. Judgment leads; the numbers support. "
    "Return STRICT JSON: {\"fire\":bool,\"confidence\":number,\"reasoning\":string,\"vetoes\":[string]}."
)


def llm_gate(inp: GateInput, model: str = "claude-haiku-4-5-20251001") -> GateDecision:
    """Live gate via Claude. Falls back to threshold_gate on any failure (no key/SDK/parse)."""
    try:
        import anthropic
        client = anthropic.Anthropic()
        payload = {
            "market_price": inp.market_price, "p_model": inp.p_model, "edge": inp.edge,
            "ev_net_fees": inp.ev_net_fees, "spread_stats": inp.spread_stats,
            "boundary_margin": inp.boundary_margin, "sigma_total": inp.sigma_total,
            "parse_warnings": inp.parse_warnings, "source_refs": inp.source_refs,
            "context": inp.context,
        }
        msg = client.messages.create(
            model=model, max_tokens=400, system=_LLM_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(payload)}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        start, end = text.find("{"), text.rfind("}")
        d = json.loads(text[start:end + 1])
        base = threshold_gate(inp)   # keep deterministic vetoes as a floor
        vetoes = list(dict.fromkeys(base.vetoes + list(d.get("vetoes", []))))
        fire = bool(d.get("fire")) and not base.vetoes
        return GateDecision(fire=fire, confidence=float(d.get("confidence", 0.0)) if fire else 0.0,
                            reasoning=str(d.get("reasoning", ""))[:500], vetoes=vetoes)
    except Exception as e:
        dec = threshold_gate(inp)
        dec.reasoning = f"llm_gate fallback ({type(e).__name__}); {dec.reasoning}"
        return dec

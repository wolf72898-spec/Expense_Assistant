"""System One (TypeSafe Jev) judgments for the R-box.

Deterministic R-box rules run in code (expense_pkg.compute). This module asks Jev only the narrow questions code
cannot decide from unstructured invoice data — declared in the core R-box `judgments` section and compiled into
policy["judgments"]. Each judgment has a guard (when to ask), a state builder (only the fields the question needs),
and an interpreter that turns the calibrated answer into an R-box effect using the judgment's confidence gate:

    auto    — answer used directly (≥ gate.auto)
    confirm — answer shown to the employee as a question (gate.floor … gate.auto)
    escalate — too uncertain; left to the agent / employee (< gate.floor)

One System One request per item carries all of that item's questions (questions are evaluated independently and in
parallel); items are sent concurrently. Answers are cached by state hash, so recomputing gaps never calls the API.
The API key comes from the environment (TYPESAFE_API_KEY), put there by the workspace secret store — never stored in
the report, the package or any log.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

HANDLERS = ("category", "buyer_title", "personal_spend", "folio_match", "credential_class")
CN_CLASSES = ("exp:DigitalInvoice", "exp:VATSpecialInvoice", "exp:VATOrdinaryInvoice", "exp:RailwayETicket",
              "exp:AirETicketItinerary")
SUPPORTING = {"payment_proof", "approval", "agenda", "boarding_pass", "other", "hotel_folio", "card_slip"}


# ───────────────────────── state builders + guards ─────────────────────────
def _norm(s: str | None) -> str:
    import re
    return re.sub(r"[\s()（）]", "", s or "").upper()


def _primary_credential(it: dict, ev_by_id: dict) -> tuple[dict | None, dict | None]:
    for e in it.get("evidence_ids", []):
        ev = ev_by_id.get(e)
        if ev and ev["kind"] not in SUPPORTING and ev.get("credential"):
            return ev, ev["credential"]
    return None, None


def _text_sample(root: Path, ev: dict, chars: int = 2500) -> str | None:
    """Text of a PDF / XML evidence file (first pages), for judgments over unstructured vouchers."""
    p = root / ev["path"]
    try:
        if p.suffix.lower() == ".pdf":
            from pypdf import PdfReader
            return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages[:3])[:chars] or None
        if p.suffix.lower() in (".xml", ".txt", ".html"):
            return p.read_text(encoding="utf-8", errors="ignore")[:chars]
    except Exception:
        return None
    return None


def build(handler: str, it: dict, r: dict, policy: dict, ev_by_id: dict, root: Path) -> dict | None:
    """Guard + state for one judgment on one item. None = not applicable (the deterministic rules suffice)."""
    ev, cred = _primary_credential(it, ev_by_id)
    inv = it.get("invoice") or {}
    lines = [li.get("item_name") for li in (cred or {}).get("line_items", []) if li.get("item_name")] or \
            [li.get("name") for li in inv.get("line_items", []) if li.get("name")]
    if handler == "category":
        if not (lines or inv.get("seller_name") or it.get("merchant")):
            return None
        return {"line_items": lines, "seller": ((cred or {}).get("seller") or {}).get("name") or inv.get("seller_name") or it.get("merchant"),
                "type_label": ((cred or {}).get("identity") or {}).get("type_label"), "description": it.get("description")}
    if handler == "buyer_title":
        title = policy.get("invoice_title") or {}
        buyer = (cred or {}).get("buyer") or {"name": inv.get("buyer_name"), "taxpayer_id": inv.get("buyer_tax_id")}
        if not title or not buyer.get("name") or buyer.get("taxpayer_id"):
            return None                               # tax ids decide it deterministically, or nothing to compare
        if _norm(buyer["name"]) == _norm(title["name"]):
            return None
        return {"buyer_on_invoice": buyer["name"], "registered_title": {"name": title["name"], "tax_id": title["tax_id"]}}
    if handler == "personal_spend":
        folio = next((ev_by_id[e] for e in it.get("evidence_ids", []) if ev_by_id.get(e, {}).get("kind") == "hotel_folio"), None)
        folio_text = _text_sample(root, folio, 3000) if folio else None
        if not lines and not folio_text:
            return None
        return {"category": it.get("category"), "line_items": lines, "folio_text": folio_text}
    if handler == "folio_match":
        if it.get("category") != "lodging" or not cred:
            return None
        folio = next((ev_by_id[e] for e in it.get("evidence_ids", []) if ev_by_id.get(e, {}).get("kind") == "hotel_folio"), None)
        if not folio:
            return None
        x = folio.get("extracted") or {}
        total = (cred.get("totals") or {}).get("total_amount")
        if x.get("balance") is not None and total is not None and abs(float(x["balance"]) - total) < 0.01:
            return None                               # structured folio already reconciles — no judgment needed
        text = _text_sample(root, folio, 3000)
        if not text:
            return None
        return {"folio_text": text, "invoice": {"seller": (cred.get("seller") or {}).get("name"), "total_amount": total},
                "stay": {k: (it.get("trip") or {}).get(k) for k in ("check_in", "check_out", "city")}}
    if handler == "credential_class":
        cands = [ev_by_id[e] for e in it.get("evidence_ids", []) if e in ev_by_id and ev_by_id[e]["kind"] not in SUPPORTING
                 and not ev_by_id[e].get("credential")]
        if not cands:
            return None
        ev = cands[0]
        return {"document": {"file_name": ev.get("original_name"), "kind_given": ev["kind"], "source_form": ev.get("source_form"),
                             "text_sample": _text_sample(root, ev, 1500)}, "_evidence_id": ev["evidence_id"]}
    return None


def question(j: dict, policy: dict) -> dict:
    q = dict(j["question"])
    if j["handler"] == "category":
        labels = {c: (spec.get("label") or c) for c, spec in (policy.get("categories") or {}).items()}
        q["criteria"] = {c: f"{lab}" for c, lab in labels.items()}
    elif j["handler"] == "credential_class":
        classes = ((policy.get("credentials") or {}).get("classes") or {})
        q["criteria"] = {c: spec.get("label", c) for c, spec in classes.items()}
    return q


def state_sha(state: dict, q: dict, model: str) -> str:
    body = json.dumps({"s": state, "q": q, "m": model}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()[:16]


# ───────────────────────── interpretation (pure; also used by compute) ─────────────────────────
def gate_of(answer: dict, gate: dict) -> tuple[str, str | bool | None, float]:
    """(band, decision, confidence). Noul: decision True/False from the probability; Choice: the label."""
    if answer["type"] == "noul":
        p = answer["noul"]
        conf = abs(p - 0.5) * 2                       # distance from indifference, 0…1
        decision = p >= 0.5
    else:
        conf = answer.get("confidence", 0.0)
        decision = answer.get("choice")
    band = "auto" if conf >= (2 * gate["auto"] - 1 if answer["type"] == "noul" else gate["auto"]) else \
           "confirm" if conf >= (2 * gate["floor"] - 1 if answer["type"] == "noul" else gate["floor"]) else "escalate"
    return band, decision, round(conf, 3)


# ───────────────────────── the API call ─────────────────────────
def _client_kwargs() -> dict:
    kw = {}
    mock = os.environ.get("EXPENSE_JEV_MOCK")            # tests: canned answers through the SDK's real HTTP layer
    if mock:
        import httpx2
        canned = json.loads(Path(mock).read_text(encoding="utf-8"))

        def handler(request):
            body = json.loads(request.content)
            answers = {}
            for name, q in body["questions"].items():
                a = canned.get(name)                          # {handler: answer} or [{"when": substring of state, "answer": …}]
                if isinstance(a, list):
                    blob = json.dumps(body["state"], ensure_ascii=False)
                    a = next((x["answer"] for x in a if x.get("when", "") in blob), None)
                if a is None:
                    a = {"type": "noul", "noul": 0.5} if q["type"] == "noul" else \
                        {"type": "choice", "choice": next(iter(q["criteria"])), "confidence": 0.1,
                         "probabilities": {k: 1 / len(q["criteria"]) for k in q["criteria"]}}
                answers[name] = a
            return httpx2.Response(200, json={"model": body.get("model") or "jev-mock", "usage": {"input_tokens": 100, "output_tokens": 5},
                                              "answers": answers}, headers={"x-typesafe-request-id": "mock-" + hashlib.sha1(request.content).hexdigest()[:8]})
        kw["http_client"] = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        kw["api_key"] = "mock"
    return kw


async def _ask_all(calls: list[tuple[str, dict, dict]], model: str | None) -> dict:
    from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy
    out = {}
    async with AsyncTypeSafeClient(model=model, retry=RetryPolicy(max_retries=2), **_client_kwargs()) as client:
        async def one(key, state, questions):
            t0 = time.perf_counter()
            resp = await client.system_one(state=state, questions=questions)
            try:
                rid = resp.request_id
            except Exception:                            # header absent (e.g. behind a gateway)
                rid = None
            out[key] = {"model": resp.model, "request_id": rid,
                        "ms": round((time.perf_counter() - t0) * 1000), "usage": resp.usage.model_dump(),
                        "answers": {n: a.model_dump(mode="json") for n, a in resp.answers.items()}}
        await asyncio.gather(*(one(k, s, q) for k, s, q in calls))
    return out


def available() -> tuple[bool, str]:
    try:
        import typesafe_sdk  # noqa: F401
    except ImportError:
        return False, "typesafe-sdk not installed (pip install typesafe-sdk)"
    if not (os.environ.get("TYPESAFE_API_KEY") or os.environ.get("EXPENSE_JEV_MOCK")):
        return False, "no TypeSafe API key (workspace secret store: `prep secret set typesafe`)"
    return True, ""


def judge(r: dict, policy: dict, root: Path, cache_path: Path, model: str | None = None, force: bool = False) -> dict:
    """Run the applicable judgments for every item; returns the report-level judgments block."""
    js = policy.get("judgments") or []
    model = model or os.environ.get("TYPESAFE_DEFAULT_MODEL") or "jev-latest"
    ev_by_id = {e["evidence_id"]: e for e in r["evidence"]}
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() and not force else {}
    plan, records = [], []
    for it in r["items"]:
        if it.get("status") == "excluded":
            continue
        qs, states = {}, {}
        for j in js:
            st = build(j["handler"], it, r, policy, ev_by_id, root)
            if st is None:
                continue
            extra = {k: st.pop(k) for k in [k for k in st if k.startswith("_")]}
            q = question(j, policy)
            sha = state_sha(st, q, model)
            rec = {"item_id": it["item_id"], "judgment_id": j["id"], "handler": j["handler"], "informs": j.get("informs", []),
                   "state_sha": sha, **({"evidence_id": extra["_evidence_id"]} if "_evidence_id" in extra else {})}
            if sha in cache:
                rec.update(cache[sha], cached=True)
                records.append(rec)
                continue
            qs[j["handler"]] = q
            states.update(st)                                    # handlers share key names, so one merged state works
            records.append(rec)
        if qs:                                                   # one request per item: shared state, many questions
            plan.append((it["item_id"], states, qs))
    ok, why = available()
    if plan and not ok:
        for rec in records:
            if "answer" not in rec:
                rec["unavailable"] = why
        return {"model": model, "records": records, "calls": 0, "unavailable": why}
    t0 = time.perf_counter()
    try:
        results = asyncio.run(_ask_all(plan, model)) if plan else {}
    except Exception as ex:                      # auth, network, rate limit … never break report processing
        why = f"{type(ex).__name__}: {str(ex)[:160]}"
        for rec in records:
            if "answer" not in rec:
                rec["unavailable"] = why
        return {"model": model, "records": records, "calls": 0, "unavailable": why}
    wall = round((time.perf_counter() - t0) * 1000)
    for rec in records:
        if "answer" in rec:
            continue
        res = results.get(rec["item_id"])
        name = rec["handler"]
        if not res or name not in res["answers"]:
            rec["unavailable"] = "no answer returned"
            continue
        rec.update({"answer": res["answers"][name], "model": res["model"], "request_id": res["request_id"],
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        cache[rec["state_sha"]] = {k: rec[k] for k in ("answer", "model", "request_id", "at")}
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"model": model, "records": records, "calls": len(plan), "wall_ms": wall,
            "api_ms": [results[k]["ms"] for k in results], "input_tokens": sum(results[k]["usage"].get("input_tokens") or 0 for k in results)}

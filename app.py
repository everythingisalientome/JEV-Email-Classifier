"""
Email classification engine on TypeSafe Jev, with a live UI.

    export OPENROUTER_API_KEY=sk-or-...      # or TYPESAFE_API_KEY for the native API
    python app.py                            # http://localhost:8080
    MOCK=1 python app.py                     # no key needed, simulated answers

Standard library only, apart from nothing. One Jev request per email; the
UI streams results over server-sent events.
"""
import json, os, random, statistics, sys, time, urllib.request, urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).parent

def load_dotenv(path=HERE / ".env"):
    """Load KEY=value lines from .env into os.environ without overriding what is already set."""
    if not path.exists(): return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1); k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)
load_dotenv()

PORT = int(os.environ.get("PORT", 8080))
MOCK = os.environ.get("MOCK") == "1"
EMAILS_FILE = HERE / os.environ.get("EMAILS_FILE", "emails.jsonl")

# Provider: OpenRouter's Decisions API (default) or TypeSafe's native endpoint.
# Both take the same body: {model, state, questions}.
if os.environ.get("TYPESAFE_API_KEY") and not os.environ.get("OPENROUTER_API_KEY"):
    PROVIDER = "typesafe"
    URL = "https://api.typesafe.ai/v1/systemone"
    KEY = os.environ["TYPESAFE_API_KEY"]
    MODEL = os.environ.get("JEV_MODEL", "jev-latest")
else:
    PROVIDER = "openrouter"
    URL = "https://openrouter.ai/api/alpha/decisions"
    KEY = os.environ.get("OPENROUTER_API_KEY", "")
    MODEL = os.environ.get("JEV_MODEL", "typesafe/jev-1.13")

PRICE_PER_M_INPUT = float(os.environ.get("JEV_PRICE_PER_M_INPUT", "0.042"))  # USD; output is $0

# ------------------------------------------------------------------ schema ---
# This is the whole "prompt". Every field is a typed question with a fixed answer space.
QUESTIONS = {
    "department": {
        "type": "choice",
        "instructions": "Which team should own this email?",
        "criteria": {
            "cards":      "Debit or credit cards: delivery, activation, declines, lost or stolen, ATM problems, wallet setup",
            "payments":   "Transfers, wires, direct deposits, bill pay, pending or missing payments, refunds not received",
            "disputes":   "Charges the customer does not recognise, duplicate charges, fees they believe are wrong",
            "fraud":      "Suspected fraud, identity theft, account takeover, phishing, request to freeze access",
            "digital":    "Online banking login, password reset, mobile app errors, account merging",
            "servicing":  "Address or profile changes, statements, letters, limits, closing accounts, product questions, rates",
            "complaints": "Dissatisfaction with service, staff or branches that needs escalation rather than a fix",
            "not_customer": "Vendor invoices, legal notices, marketing, newsletters, business partnerships, system notifications",
        },
    },
    "urgency": {
        "type": "score",
        "instructions": "How quickly does this need a response?",
        "criteria": ["No response needed", "Within a week", "Within one business day", "Same day, money or security at risk"],
    },
    "needs_reply":  {"type": "noul", "instructions": "Does the sender expect a written reply from the bank?"},
    "is_complaint": {"type": "noul", "instructions": "Is the sender expressing dissatisfaction with the bank's service?"},
    "has_pii":      {"type": "noul", "instructions": "Does the email contain sensitive identifiers such as a social security number, full card number or password?"},
    "is_phishing":  {"type": "noul", "instructions": "Is this email itself a phishing or scam attempt directed at the reader?"},
}

# ------------------------------------------------------------ comparison ----
# What the same run would cost on a chat LLM, estimated: (prompt + email) in, a JSON reply out,
# priced at OpenRouter list rates. Estimated, never called. Prices in USD per million tokens.
COMPARE_MODELS = [
    {"name": "Gemini 2.5 Flash", "id": "google/gemini-2.5-flash", "in": 0.30, "out": 2.50},
    {"name": "GPT-4o mini",      "id": "openai/gpt-4o-mini",      "in": 0.15, "out": 0.60},
]
LLM_OUTPUT_TOKENS = 90   # a JSON reply with six fields and six confidences

def llm_prompt_text() -> str:
    """The prompt you would write for a chat LLM to do the same job. Built from QUESTIONS so it stays in sync."""
    d, u = QUESTIONS["department"], QUESTIONS["urgency"]
    lines = ["You are an email triage assistant for a retail bank. Read the customer email and classify it.", "",
             "Department must be exactly one of: " + ", ".join(d["criteria"]) + "."]
    lines += [f"- {k}: {v}" for k, v in d["criteria"].items()]
    lines += ["", "Urgency is an integer 0-3:"] + [f"- {i}: {c}" for i, c in enumerate(u["criteria"])] + [""]
    lines += [f"{k} (true/false): {QUESTIONS[k]['instructions']}" for k in ("needs_reply", "is_complaint", "has_pii", "is_phishing")]
    lines += ["", "For each field also give a confidence between 0 and 1.", "",
              "Respond ONLY with valid JSON in this exact shape, no prose, no markdown, no explanation:",
              '{"department": "...", "urgency": 0, "needs_reply": true, "is_complaint": false, "has_pii": false, "is_phishing": false, "confidence": {...}}']
    return "\n".join(lines)

def est_tokens(text: str) -> int:
    return max(1, len(text) // 4)

LLM_PROMPT_TOKENS = est_tokens(llm_prompt_text())

def llm_cost_for(email_state: dict) -> dict:
    tin = LLM_PROMPT_TOKENS + est_tokens(json.dumps(email_state, ensure_ascii=False))
    return {m["name"]: (tin * m["in"] + LLM_OUTPUT_TOKENS * m["out"]) / 1e6 for m in COMPARE_MODELS}

# ------------------------------------------------------------------ policy ---
# This is the enterprise control boundary. Jev reports; this code decides.
# Thresholds are ours: versioned, reviewable, and tuned on labeled data, never
# inside the model. Defaults can be overridden per run from the UI.
AUTO_THRESHOLD = float(os.environ.get("AUTO_THRESHOLD", "0.90"))    # >= this: route automatically
REVIEW_THRESHOLD = float(os.environ.get("REVIEW_THRESHOLD", "0.75"))  # >= this: route, but a person verifies
FLAG_THRESHOLD = 0.50                                                # noul flags: P(true) above this counts

def decide(answers: dict, auto=AUTO_THRESHOLD, review=REVIEW_THRESHOLD) -> dict:
    """Return {route, action, reason}. action is one of AUTO, VERIFY, HUMAN."""
    dept = answers.get("department", {}); p = dept.get("p")
    flags = {k: answers.get(k, {}) for k in ("is_phishing", "has_pii", "is_complaint")}
    is_true = lambda k: flags[k].get("value") is True

    # Hard rules first: some outcomes escalate no matter how confident the routing is.
    if is_true("is_phishing"):
        return {"route": "security", "action": "HUMAN", "reason": "phishing flag set, never auto-handled"}
    if is_true("has_pii"):
        return {"route": dept.get("value") or "triage", "action": "HUMAN", "reason": "contains PII, needs handling under data policy"}
    if is_true("is_complaint") and dept.get("value") != "complaints":
        return {"route": dept.get("value") or "triage", "action": "VERIFY", "reason": "complaint flagged outside complaints queue"}

    # Then confidence bands on the routing decision.
    if p is None:
        return {"route": "triage", "action": "HUMAN", "reason": "no answer from model"}
    if p >= auto:
        return {"route": dept["value"], "action": "AUTO", "reason": f"department confidence {p:.2f} >= {auto:.2f}"}
    if p >= review:
        return {"route": dept["value"], "action": "VERIFY", "reason": f"confidence {p:.2f} in review band [{review:.2f}, {auto:.2f})"}
    return {"route": "triage", "action": "HUMAN", "reason": f"confidence {p:.2f} < {review:.2f}, routing unclear"}

# ------------------------------------------------------------------ client ---

def call_jev(state: dict) -> dict:
    body = json.dumps({"model": MODEL, "state": state, "questions": QUESTIONS}).encode()
    req = urllib.request.Request(URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost", "X-Title": "jev-email-demo"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 529) and attempt < 4:
                time.sleep(float(e.headers.get("Retry-After", 2 ** attempt))); continue
            raise RuntimeError(f"{e.code}: {e.read()[:300].decode(errors='replace')}")

_warned = set()

def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None

def _p_true(a: dict):
    """Extract P(true) from a noul answer, whatever shape the provider uses."""
    for k in ("noul", "p_true", "probability_true", "true_probability", "prob_true"):  # TypeSafe: {"type": "noul", "noul": 0.89}
        if _num(a.get(k)) is not None: return _num(a[k])
    dist = a.get("probabilities") or a.get("distribution") or a.get("probability")
    if isinstance(dist, dict):
        for k in ("true", "True", "yes", True):
            if _num(dist.get(k)) is not None: return _num(dist[k])
        if _num(dist.get("false")) is not None: return 1 - _num(dist["false"])
    value = a.get("value", a.get("answer", a.get("result")))
    prob = _num(a.get("probability")) if _num(a.get("probability")) is not None else _num(a.get("confidence"))
    if isinstance(value, bool):
        return (prob if value else 1 - prob) if prob is not None else (1.0 if value else 0.0)
    if _num(value) is not None: return _num(value)        # value itself is P(true)
    if isinstance(value, str) and value.lower() in ("true", "false", "yes", "no"):
        v = value.lower() in ("true", "yes")
        return (prob if v else 1 - prob) if prob is not None else (1.0 if v else 0.0)
    return prob

def parse(resp: dict) -> dict:
    """Normalise answers to {name: {value, p}} regardless of exact field naming."""
    out = {}
    for name, q in QUESTIONS.items():
        a = (resp.get("answers") or {}).get(name, {}) or {}
        if not isinstance(a, dict): a = {"value": a}
        dist = a.get("probabilities") or a.get("distribution")
        value = a.get("value", a.get("answer", a.get("choice", a.get("selected"))))
        p = _num(a.get("probability")) if _num(a.get("probability")) is not None else _num(a.get("confidence"))
        if q["type"] == "noul":
            pt = _p_true(a)
            value = bool(pt is not None and pt >= 0.5)
            p = None if pt is None else (pt if value else 1 - pt)   # confidence in the stated answer
        elif q["type"] == "score":
            seq = list(dist.values()) if isinstance(dist, dict) else dist if isinstance(dist, list) else None
            if value is None and seq: value = max(range(len(seq)), key=lambda i: seq[i])
            if _num(value) is not None:
                idx = int(round(_num(value))); value = q["criteria"][max(0, min(idx, len(q["criteria"]) - 1))]
            if p is None and seq: p = max(seq)
        else:
            if value is None and isinstance(dist, dict) and dist: value = max(dist, key=dist.get)
            if p is None and isinstance(dist, dict) and _num(dist.get(value)) is not None: p = _num(dist[value])
        if p is None and name not in _warned and a:
            _warned.add(name); print(f"[parse] no probability found for {name}; raw answer: {json.dumps(a)[:300]}", file=sys.stderr)
        out[name] = {"value": value, "p": round(p, 3) if p is not None else None}
    return out

def mock_jev(state: dict) -> dict:
    text = (state["subject"] + " " + state["body"]).lower()
    rules = [("fraud", ["fraud","stolen","freeze","identity","didn't make","lost my phone","opened an account"]),
             ("disputes", ["twice","duplicate","don't know","nicht kenne","fee seems","overdraft","markup","looks off"]),
             ("cards", ["card","atm","apple pay","withdrawal limit","japan"]),
             ("payments", ["transfer","wire","direct deposit","refund","bounced","mortgage","loan payment","pending charge"]),
             ("digital", ["app","password","login","logins","locked"]),
             ("complaints", ["unacceptable","manager","third time","complaint"]),
             ("not_customer", ["invoice","subpoena","newsletter","demo","partnership","settlement","statement is ready"])]
    dept = "servicing"
    for d, kws in rules:
        if any(k in text for k in kws): dept = d; break
    urgent = any(k in text for k in ["urgent","today","immediately","freeze","friday","tomorrow","stolen"])
    spam = dept == "not_customer"
    j = lambda lo, hi: round(random.uniform(lo, hi), 3)
    time.sleep(random.uniform(0.08, 0.25))
    return {"model": "mock", "usage": {"input_tokens": len(text) // 4 + 260, "output_tokens": 0},
            "answers": {
                "department":  {"value": dept, "probability": j(0.55, 0.95) if dept == "servicing" else j(0.84, 0.995)},
                "urgency":     {"value": 3 if urgent else (0 if spam else random.choice([1, 2])), "probability": j(0.6, 0.95)},
                "needs_reply": {"probability": j(0.02, 0.2) if spam else j(0.85, 0.99)},
                "is_complaint":{"probability": j(0.7, 0.98) if dept == "complaints" else j(0.02, 0.35)},
                "has_pii":     {"probability": j(0.9, 0.99) if "ssn" in text or "-44-" in text else j(0.01, 0.1)},
                "is_phishing": {"probability": j(0.9, 0.99) if "verify your identity within" in text else j(0.01, 0.06)},
            }}

# ------------------------------------------------------------------ server ---

def load_emails():
    rows = [json.loads(l) for l in EMAILS_FILE.open(encoding="utf-8") if l.strip()]
    for i, r in enumerate(rows, 1):
        r.setdefault("id", i)
    return rows

def state_of(email: dict) -> dict:
    """What Jev sees. Never includes id or label."""
    return {k: email[k] for k in ("from", "subject", "body") if k in email}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            data = (HERE / "static" / "index.html").read_bytes()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        elif u.path == "/schema":
            self.send_json({"questions": QUESTIONS, "model": MODEL, "provider": "mock" if MOCK else PROVIDER,
                            "price_per_m_input": PRICE_PER_M_INPUT,
                            "auto_threshold": AUTO_THRESHOLD, "review_threshold": REVIEW_THRESHOLD,
                            "llm_prompt": llm_prompt_text(), "llm_prompt_tokens": LLM_PROMPT_TOKENS,
                            "llm_output_tokens": LLM_OUTPUT_TOKENS, "compare_models": COMPARE_MODELS})
        elif u.path == "/stream":
            self.stream(parse_qs(u.query))
        else:
            self.send_response(404); self.end_headers()

    def send_json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def stream(self, qs):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream"); self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        emails = load_emails()
        limit = int(qs.get("limit", [len(emails)])[0])
        auto = float(qs.get("auto", [AUTO_THRESHOLD])[0]); review = float(qs.get("review", [REVIEW_THRESHOLD])[0])
        latencies, tokens, cost, t0 = [], 0, 0.0, time.perf_counter()
        actions = {"AUTO": 0, "VERIFY": 0, "HUMAN": 0}
        llm_cost = {m["name"]: 0.0 for m in COMPARE_MODELS}
        self.emit("start", {"total": min(limit, len(emails)), "model": MODEL, "provider": "mock" if MOCK else PROVIDER})
        for email in emails[:limit]:
            self.emit("email", email)
            t = time.perf_counter()
            try:
                resp = mock_jev(email) if MOCK else call_jev(state_of(email))
                answers = parse(resp)
                err = None
            except Exception as e:
                answers, resp, err = {}, {}, str(e)
            ms = (time.perf_counter() - t) * 1000
            usage = resp.get("usage") or {}
            in_tok = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            if not in_tok: in_tok = len(json.dumps(state_of(email))) // 4 + len(json.dumps(QUESTIONS)) // 4  # estimate
            tokens += in_tok; cost += in_tok / 1e6 * PRICE_PER_M_INPUT; latencies.append(ms)
            decision = decide(answers, auto, review); actions[decision["action"]] += 1
            for k, v in llm_cost_for(state_of(email)).items(): llm_cost[k] += v
            self.emit("result", {"id": email["id"], "answers": answers, "error": err, "latency_ms": round(ms),
                                 "decision": decision, "expected": email.get("label"),
                                 "input_tokens": in_tok,
                                 "totals": {"classified": len(latencies), "elapsed_s": round(time.perf_counter() - t0, 2),
                                            "cost_usd": round(cost, 6), "tokens": tokens,
                                            "median_ms": round(statistics.median(latencies)),
                                            "actions": actions,
                                            "llm_cost_usd": {k: round(v, 6) for k, v in llm_cost.items()}}})
        self.emit("done", {"classified": len(latencies), "elapsed_s": round(time.perf_counter() - t0, 2),
                           "cost_usd": round(cost, 6), "actions": actions,
                           "llm_cost_usd": {k: round(v, 6) for k, v in llm_cost.items()}})

    def emit(self, event, data):
        try:
            self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            raise SystemExit

if __name__ == "__main__":
    if not MOCK and not KEY:
        sys.exit("set OPENROUTER_API_KEY (or TYPESAFE_API_KEY), or run with MOCK=1")
    print(f"provider={'mock' if MOCK else PROVIDER} model={MODEL} emails={EMAILS_FILE.name} ({len(load_emails())})  ->  http://localhost:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

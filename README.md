# Jev email classification demo

Streams an inbox through TypeSafe's Jev (a System One model), one request per
email, and shows each decision as it lands: department, urgency, four flags,
and the policy action our code takes on it. Running totals for emails
classified, elapsed time, cost, median latency, and an approximate cost for
the same run on two chat LLMs.

Python 3.10+, standard library only. No packages to install.

## Run
    cp .env.example .env            # add your key
    python probe.py                 # one email, prints Jev's raw response
    python app.py                   # open http://localhost:8080

`.env` is read at startup. `OPENROUTER_API_KEY` uses Jev via OpenRouter
(default); `TYPESAFE_API_KEY` uses TypeSafe's native endpoint. `MOCK=1`
runs the UI with simulated answers and no API calls.

## Data
- `emails.jsonl`: 36 hand-written sample emails (one phishing, one with a fake SSN, one in German).
- `bank_inbound_emails.jsonl`: 100 synthetic labeled emails, generated from the prompt in `docs/`-less form below; wider length spread, quoted threads, 20 non-English.

Set `EMAILS_FILE` in `.env` to choose. Rows need `from`, `subject`, `body`;
`id` is assigned if missing; `label` (expected department) is optional and
is shown in the UI as a mismatch marker. Jev never sees `id` or `label`.

All emails are synthetic. Never commit real customer email.

## How Jev is called
No prompt. Request = `{model, state, questions}`.

- `state`: the email as `{from, subject, body}`.
- `questions` (`QUESTIONS` in app.py): the schema. Each field is a typed question:

| field        | type   | answer space |
|--------------|--------|--------------|
| department   | choice | cards, payments, disputes, fraud, digital, servicing, complaints, not_customer |
| urgency      | score  | 4 ordered levels |
| needs_reply  | noul   | P(true) |
| is_complaint | noul   | P(true) |
| has_pii      | noul   | P(true) |
| is_phishing  | noul   | P(true) |

All six are answered in one call. Jev returns typed values with probabilities;
nothing is generated or parsed. Response shapes as observed on OpenRouter
(`typesafe/jev-1.13`): choice and score answers carry `value` and
`probability`; noul answers are `{"type": "noul", "noul": <P(true)>}`.

The criteria descriptions are the only tuning lever. When two departments
get confused, sharpen their two descriptions; don't add prompts or examples.

## Escalation policy (`decide()` in app.py)
Jev reports; code decides. In order:
1. `is_phishing` true → HUMAN (security), always
2. `has_pii` true → HUMAN, always
3. `is_complaint` true outside the complaints queue → VERIFY
4. department confidence ≥ auto threshold (default .90) → AUTO
5. ≥ verify threshold (default .75) → VERIFY: routed, a person checks
6. otherwise → HUMAN (triage)

Thresholds: `AUTO_THRESHOLD`, `REVIEW_THRESHOLD` in `.env`, adjustable per
run from the UI. They are policy, versioned in code, and should be set from a
labeled calibration run, not by feel.

## Cost
Jev: `usage.input_tokens` × $0.042 per million (output is free). Override with
`JEV_PRICE_PER_M_INPUT`.

Chat-LLM comparison (`COMPARE_MODELS` in app.py): estimated, never called.
Assumes the equivalent prompt (built from `QUESTIONS`, shown on tab 1) plus
each email in, ~90 tokens of JSON out, at OpenRouter list prices. Marked ≈ in
the UI. Time is deliberately not compared.

## UI
- Tab 1, What we ask: the prompt you'd write for a chat LLM next to the six Jev questions. Both render from `QUESTIONS`.
- Tab 2, Run: grid fills row by row as Jev answers. Click a row for the full email, all six answers with probabilities, the policy reason, and the expected label if present. Right panel: totals, cost comparison, routing outcome, threshold sliders.

## Files
    app.py               server, Jev client, schema, policy, cost estimate, mock
    probe.py             one-email diagnostic
    static/index.html    the UI, single file
    emails.jsonl         36 samples
    bank_inbound_emails.jsonl   100 labeled samples
    .env.example         config template

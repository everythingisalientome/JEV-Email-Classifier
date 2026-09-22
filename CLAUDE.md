# jev-email-demo

Email classification engine on TypeSafe Jev (System One model) with a live
presentation UI. Standard library only; keep it that way unless a dependency
removes real complexity. Read README.md first.

## Run
- `python probe.py [id]`: one email, raw Jev response, parsed answers, decision
- `python app.py`: http://localhost:8080, reads .env
- `MOCK=1 python app.py`: simulated answers, no key. Mock probabilities are
  random; never draw conclusions from a mock run.

## How Jev is called
- No prompt. Request = `{model, state, questions}`.
- `state` = `state_of(email)` = `{from, subject, body}`. Never add `id`,
  `label`, or instructions to state.
- `questions` = `QUESTIONS`. Types: choice / score / noul. Criteria
  descriptions are the only tuning lever.
- Observed response shape: choice/score answers have `value` + `probability`;
  noul answers are `{"type": "noul", "noul": <P(true)>}`. `_p_true()` handles
  this first; other branches are fallbacks for other providers.
- Cost = `usage.input_tokens` × price; output is free.

## Rules
- Policy lives in `decide()`: thresholds, routing, hard escalations (phishing,
  PII, complaint). Never move policy into criteria or into the model.
- No system prompt, persona, or few-shot examples anywhere. Jev has no place
  for them.
- When two departments get confused, rewrite their two criteria descriptions.
  Don't add questions or fallback prompts.
- `llm_prompt_text()` is the chat-LLM equivalent shown on tab 1 and used for
  the cost estimate. It derives from `QUESTIONS`; don't hand-edit it.
- Chat-LLM comparison is cost only, estimated, marked ≈. Do not add latency
  or accuracy claims for models we didn't run.
- UI is one HTML file, no build step, no framework.
- Data files are synthetic. Never commit real customer email or a real key.

## Verified
- OpenRouter `POST /api/alpha/decisions`, model `typesafe/jev-1.13`, works
  with the request shape in `call_jev()`.
- Phishing and PII escalations fire (emails 6 and 10 in emails.jsonl).

## Open items
- Accuracy and per-band calibration report against `label` (not built).
- `has_pii` scores 0.60–0.67 on emails that describe a card number as a test
  card. Policy question, not a bug; decide before changing the criteria.

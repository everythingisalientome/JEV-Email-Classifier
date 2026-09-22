"""Send one email to Jev and print the raw response, then what parse() makes of it.
   OPENROUTER_API_KEY=sk-or-... python probe.py [email_id]"""
import json, sys, app
emails = app.load_emails()
email = next(e for e in emails if e["id"] == int(sys.argv[1] if len(sys.argv) > 1 else 1))
print("provider:", app.PROVIDER, "model:", app.MODEL, "\nemail:", email["subject"], "\n")
resp = app.call_jev(email)
print("RAW RESPONSE\n" + json.dumps(resp, indent=2))
print("\nPARSED\n" + json.dumps(app.parse(resp), indent=2))
print("\nDECISION\n" + json.dumps(app.decide(app.parse(resp)), indent=2))

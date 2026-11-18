# ResolveAI

A terminal-based decision-making resolution agent implementing the supplied assignment with the Gemini LLM API and SQLite. Python 3.10 or later is required. There are no third-party dependencies and no code comments.

## Run

From this project directory in PowerShell:

```powershell
$env:GEMINI_API_KEY="your-gemini-api-key"
python main.py "My headphones from ORD-1001 arrived damaged. Please resolve this."
```

Alternatively, copy `.env.example` to `.env` and enter your key there. The program loads it automatically. The default model is `gemini-3.8-flash`; set `GEMINI_MODEL` to another function-calling Gemini model available to your account if needed.

```powershell
Copy-Item .env.example .env
```

Run without a request to enter the issue at a terminal prompt:

```powershell
python main.py
```

The Gemini integration follows Google's [function-calling documentation](https://ai.google.dev/gemini-api/docs/function-calling) and [generateContent API contract](https://ai.google.dev/api/generate-content). Opaque function-call signatures are preserved across turns.

## Assignment workflow

1. Gemini receives the customer request and the declarative tool contracts.
2. `lookup_order` retrieves the order, customer identity, purchased item, order status and exchange eligibility.
3. `inspect_inventory` checks current warehouse stock.
4. `confirm_exchange` reserves a replacement if the order is eligible and stock is available. Otherwise, `process_refund` records the full refund amount and transaction reference against the original payment method.
5. `notify_customer` dispatches the confirmation to the simulated local mailbox. Gemini then summarizes the outcome.

The centralized dispatcher only executes registered tools, validates JSON arguments, and returns structured success/error payloads. The agent maintains ordered user, model and tool messages with matching call IDs. Malformed arguments and tool failures are returned to Gemini for correction. The loop stops after five model iterations and flags unfinished sessions for human assistance. A final model answer alone cannot mark incomplete fulfillment as successful.

## Sample orders

The first run creates `data/resolveai.db` with these records:

| Order | Item | Replacement stock | Exchange eligible | Expected resolution |
| --- | --- | --- | --- | --- |
| ORD-1001 | Wireless Headphones | 8 | Yes | Exchange |
| ORD-1002 | Mechanical Keyboard | 0 | Yes | Refund INR 3,999.00 |
| ORD-1003 | Smart Desk Lamp | 5 | No | Refund INR 1,799.00 |
| ORD-9999 | Does not exist | - | - | Ask customer to verify ID |

```powershell
python main.py "My keyboard from ORD-1002 does not work."
python main.py "The lamp in ORD-1003 arrived broken."
python main.py "My item from ORD-9999 is damaged."
```

Stock, resolutions, refund references, confirmation emails and session histories persist in SQLite. Retrying an already resolved order reuses its resolution and notification. Stock and policy are rechecked inside the resolution transaction.

Refunds, exchanges and email delivery are local simulations for the assignment. No real money is transferred, shipment booked or external email sent. Confirmation emails are recorded in the database's `notifications` table. Sessions needing human assistance are recorded with `needs_human` in the `runs` table. Only Gemini calls use an external service.

## Verify

```powershell
python -m unittest discover -s tests -v
```

Tests cover the assignment's exchange, refund, ineligibility and invalid-order scenarios, plus safe dispatch, argument correction, loop limits, notifications and the Gemini function-call protocol. Tests mock Gemini responses and need no API key.

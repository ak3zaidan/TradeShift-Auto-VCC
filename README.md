# Tradeshift VCC Generator

A Python automation tool for generating and exporting Virtual Credit Cards (VCCs) from [Tradeshift Go](https://getgo.tradeshift.com) via browser session capture and the Tradeshift REST API.

---

## Features

- **Browser-based login** — launches a Chromium window via `nodriver`, waits for you to sign in, then captures your authenticated session (cookies + user agent) automatically
- **Bulk VCC creation** — generates any number of recurring-use virtual cards ($5,000 USD daily limit, 4-year expiry by default) via the full create → submit → approve API flow
- **Wallet export** — fetches all existing active cards from your wallet and exports full card details (PAN, CVV, expiry) to a CSV
- **Rate-limit aware** — automatically backs off 30s on HTTP 429 responses and adds polite delays between requests
- **Persistent log** — appends a timestamped record of every created card to `tradeshift_cards.txt`

---

## Requirements

- Python 3.9+
- A Tradeshift Go account with at least one active team and card source
- The following Python packages:
  - `requests`
  - `nodriver`
- A local `client` module providing `NoDriverClient`, `BrowserType`, and `WindowConfig`

Install dependencies:

```bash
pip install requests nodriver
```

---

## Usage

```bash
python tradeshift.py
```

On launch, a Chromium browser window will open and navigate to the Tradeshift Go login page. Sign in manually — the script waits up to 100 seconds for your dashboard to load, then captures your session automatically.

You'll then be prompted to choose an action:

```
What would you like to do?
  1) Create new virtual cards
  2) Fetch all existing cards to cards.csv
Enter 1 or 2:
```

### Option 1 — Create new virtual cards

Enter the number of cards to generate. Each card is created with these defaults:

| Setting     | Value              |
|-------------|--------------------|
| Type        | `recurring-use`    |
| Amount      | $5,000 USD         |
| Frequency   | Daily              |
| End date    | Today + 4 years    |
| Description | `Company`          |

Progress is printed per card, and request IDs are appended to `tradeshift_cards.txt`.

### Option 2 — Fetch existing cards to CSV

Pages through your entire wallet and exports full card details to `cards.csv`:

| Column           | Description              |
|------------------|--------------------------|
| Card Holder Name | Account full name/email  |
| Card Number      | Full PAN                 |
| Exp Month        | Expiry month (MM)        |
| Exp Year         | Expiry year (YYYY)       |
| CVV              | Card verification value  |

---

## Output Files

| File                   | Contents                                              |
|------------------------|-------------------------------------------------------|
| `tradeshift_cards.txt` | Timestamped log of created card request IDs           |
| `cards.csv`            | Full card details (PAN/CVV/expiry) for wallet export  |

Both files are written to the same directory as the script.

---

## Configuration

Key constants at the top of `tradeshift.py` can be adjusted:

```python
CARD_AMOUNT          = "5000"       # Spend limit per cycle
CARD_CURRENCY        = "USD"        # Currency
CARD_FREQUENCY       = "DAILY"      # Reset frequency
CARD_END_DATE_YEARS  = 4            # Years until card expiry
LOGIN_TIMEOUT_S      = 100          # Seconds to wait for login
WALLET_PAGE_SIZE     = 50           # Cards fetched per API page
```

---

## How It Works

1. **Session capture** — `nodriver` drives Chromium to the login page. Once the dashboard sidebar element is detected via XPath, all browser cookies are extracted via CDP and loaded into a `requests.Session`.
2. **Account discovery** — the script calls `/external/rest/user` and `/external/rest/teams` to resolve the user ID, team ID, and card source ID needed for card creation.
3. **Card generation** — for each card, the script:
   - Creates a draft request (`POST /external/rest/requests/{uuid}`)
   - Submits it with card parameters (`POST .../submit`)
   - Polls the conversation events feed for the `purchases.approvalTask` event
   - Self-approves the request (`POST .../approve`)
4. **Card detail export** — for each wallet entry, a short-lived JWT is minted via `/external/rest/payments/card`, then used to call the issuer gateway and retrieve the full PAN, CVV, and expiry.

---

## Notes

- Self-approval requires that your Tradeshift Go account has manager/approver permissions on the team.
- The card gateway hostname is derived from the `tokenSource` field (e.g. `fintech-cards-amex`).
- MFA or SSO flows that redirect outside of Tradeshift Go may prevent session capture — complete all auth steps before the 100s timeout.

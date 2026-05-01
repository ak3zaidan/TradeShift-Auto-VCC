from __future__ import annotations

import asyncio
import csv
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from nodriver import cdp

from client import (
    BrowserType,
    NoDriverClient,
    WindowConfig,
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LOGIN_URL = "https://getgo.tradeshift.com/signin/login"
API_BASE = "https://getgo-api.tradeshift.com"
WEB_ORIGIN = "https://getgo.tradeshift.com"

# `data-testid="sideMenu-dashboard-btn"` lives on the dashboard side-menu link
# that only renders once the user is fully authenticated.
DASHBOARD_XPATH = '//a[@data-testid="sideMenu-dashboard-btn"]'

LOGIN_TIMEOUT_S = 100

# Card defaults. Subscription = recurring-use card type in Tradeshift Go.
CARD_TYPE = "recurring-use"
CARD_AMOUNT = "5000"
CARD_CURRENCY = "USD"
CARD_FREQUENCY = "DAILY"
CARD_DESCRIPTION = "Company"
CARD_END_DATE_YEARS = 4  # endDate = today + 4 years (matches captured flow)

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tradeshift_cards.txt")
CARDS_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cards.csv")

# Wallet listing pagination. Browser uses 12; we bump it to 50 to cut roundtrips.
WALLET_PAGE_SIZE = 50

# ANSI colors
GREEN = "\033[92m"
RED = "\033[91m"
ORANGE = "\033[38;5;208m"
CYAN = "\033[96m"
RESET = "\033[0m"


# ---------------------------------------------------------------------------
# Browser session capture
# ---------------------------------------------------------------------------

async def _extract_cookies(client: NoDriverClient) -> List[Any]:
    """Pull all cookies from the browser via CDP, returned as requests-ready
    `http.cookiejar.Cookie` objects.

    We can't use `browser.cookies.get_all()` because the installed nodriver
    version touches `tab.closed`, which doesn't exist on the Tab type.
    """
    import http.cookiejar  # noqa: WPS433
    import requests.cookies  # noqa: WPS433

    connection = client.current_tab
    if connection is None:
        for tab in client.browser.tabs:
            connection = tab
            break
    if connection is None:
        connection = client.browser.connection

    raw_cookies = await connection.send(cdp.storage.get_cookies())

    out: List[http.cookiejar.Cookie] = []
    for c in raw_cookies:
        try:
            out.append(
                requests.cookies.create_cookie(
                    name=c.name,
                    value=c.value,
                    domain=c.domain,
                    path=c.path,
                    expires=c.expires if getattr(c, "expires", None) else None,
                    secure=bool(getattr(c, "secure", False)),
                )
            )
        except Exception:
            continue
    return out


async def capture_session() -> Dict[str, Any]:
    """Open a browser, wait for the user to sign in, return cookies + UA.

    Returns a dict with:
        cookies     -> list of http.cookiejar.Cookie objects (requests-ready)
        user_agent  -> the navigator.userAgent string from the browser
    """
    client = NoDriverClient(
        browser_type=BrowserType.CHROMIUM,
        window_config=WindowConfig(width=1280, height=900),
        custom_args=["--disable-breakpad"],
        use_dynamic_proxy=False,
        block_user_interaction=False,
    )

    print(f"{CYAN}Launching browser...{RESET}")
    await client.start()
    await asyncio.sleep(2)

    try:
        await client.navigate(LOGIN_URL, wait_for_load=False)
        print(
            f"{CYAN}Sign in to Tradeshift in the opened window. "
            f"Waiting up to {LOGIN_TIMEOUT_S}s for the dashboard to load...{RESET}"
        )

        loop = asyncio.get_event_loop()
        deadline = loop.time() + LOGIN_TIMEOUT_S
        found = False
        while loop.time() < deadline:
            try:
                el = await client.find_element(
                    DASHBOARD_XPATH, by="xpath", timeout=1.0
                )
            except Exception:
                el = None
            if el is not None:
                found = True
                break
            await asyncio.sleep(0.5)

        if not found:
            raise TimeoutError(
                f"Timed out after {LOGIN_TIMEOUT_S}s waiting for the dashboard "
                "side-menu button. The user did not finish signing in."
            )

        print(f"{GREEN}Login detected.{RESET}")

        cookies = await _extract_cookies(client)
        try:
            user_agent = await client.get_user_agent()
        except Exception:
            user_agent = None

        return {"cookies": cookies, "user_agent": user_agent or ""}
    finally:
        try:
            await asyncio.wait_for(client.close(), timeout=10.0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# requests.Session helpers
# ---------------------------------------------------------------------------

def build_session(captured: Dict[str, Any]) -> requests.Session:
    """Build a requests.Session that mimics the browser's auth context."""
    session = requests.Session()

    for cookie in captured["cookies"]:
        try:
            session.cookies.set_cookie(cookie)
        except Exception:
            pass

    if not any(c.name == "tsgotoken" for c in session.cookies):
        raise RuntimeError(
            "Did not capture a `tsgotoken` cookie from the browser. The "
            "Tradeshift session is missing or expired."
        )

    ua = captured.get("user_agent") or (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/26.2 Safari/605.1.15"
    )

    session.headers.update({
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": WEB_ORIGIN,
        "Referer": WEB_ORIGIN + "/",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    })

    return session


def _ts_request_id() -> str:
    """Fresh idempotency UUID for the `x-tradeshift-requestid` header."""
    return str(uuid.uuid4())


def api_get(session: requests.Session, path: str) -> Any:
    url = API_BASE + path
    resp = session.get(url, headers={"x-tradeshift-requestid": _ts_request_id()}, timeout=30)
    resp.raise_for_status()
    if not resp.content:
        return None
    return resp.json()


def api_post(session: requests.Session, path: str, body: Dict[str, Any]) -> Any:
    url = API_BASE + path
    resp = session.post(
        url,
        json=body,
        headers={
            "Content-Type": "application/json",
            "x-tradeshift-requestid": _ts_request_id(),
        },
        timeout=30,
    )
    resp.raise_for_status()
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return resp.text


# ---------------------------------------------------------------------------
# Account discovery
# ---------------------------------------------------------------------------

def fetch_account_info(session: requests.Session) -> Dict[str, Any]:
    """Look up the userId, teamId, and the team's primary cardSourceId."""
    user = api_get(session, "/external/rest/user")
    if not isinstance(user, dict):
        raise RuntimeError(f"Unexpected /user response: {user!r}")

    user_id = user.get("userId") or user.get("Id") or user.get("id")
    user_email = user.get("email") or user.get("Username") or "?"
    first_name = user.get("firstName") or user.get("FirstName") or ""
    last_name = user.get("lastName") or user.get("LastName") or ""
    full_name = (f"{first_name} {last_name}").strip() or user.get("fullName") or user_email
    if not user_id:
        raise RuntimeError(f"Could not extract userId from /user: {user!r}")

    teams = api_get(session, "/external/rest/teams?state=active&limit=10&offset=0")
    if not isinstance(teams, list) or not teams:
        raise RuntimeError("No active teams returned from /teams")

    # Prefer a team that already has a card source attached.
    team = next(
        (t for t in teams if isinstance(t.get("sourceCards"), list) and t["sourceCards"]),
        teams[0],
    )

    source_cards = team.get("sourceCards") or []
    if not source_cards:
        raise RuntimeError(
            f"Team '{team.get('name')}' has no card sources. Add one in the "
            "Tradeshift dashboard before generating virtual cards."
        )
    card_source = source_cards[0]

    return {
        "user_id": user_id,
        "user_email": user_email,
        "user_full_name": full_name,
        "team_id": team["id"],
        "team_name": team.get("name", "?"),
        "card_source_id": card_source["id"],
        "card_source_label": card_source.get("label") or card_source.get("name", "?"),
    }


# ---------------------------------------------------------------------------
# Card generation flow
# ---------------------------------------------------------------------------

def _end_date() -> str:
    """endDate = today + 4 years, formatted YYYY-MM-DD."""
    target = datetime.now(timezone.utc).date() + timedelta(days=365 * CARD_END_DATE_YEARS)
    return target.strftime("%Y-%m-%d")


def _create_draft_request(session: requests.Session, request_id: str) -> None:
    """Create a brand-new draft request resource on the server.

    Mirrors what the Tradeshift Go UI does when you click "Create new request":
        POST /external/rest/requests/{client-generated-uuid}  (empty body)
    The server responds with `{}` and the resource then exists in CREATED
    state, ready for `/submit`.
    """
    url = API_BASE + f"/external/rest/requests/{request_id}"
    resp = session.post(
        url,
        data=b"",  # empty body, but Content-Length must be 0 (not omitted)
        headers={
            "Content-Length": "0",
            "x-tradeshift-requestid": _ts_request_id(),
        },
        timeout=30,
    )
    resp.raise_for_status()


def _wait_for_approval_task(
    session: requests.Session,
    request_id: str,
    timeout_s: float = 30.0,
) -> str:
    """Poll the request's events feed until the approvalTask shows up."""
    deadline = time.time() + timeout_s
    last_err: Optional[str] = None

    while time.time() < deadline:
        try:
            data = api_get(
                session,
                f"/external/rest/conversations/{request_id}/events?polling=false",
            )
            events = (data or {}).get("events", []) if isinstance(data, dict) else []
            for ev in events:
                if ev.get("type") == "purchases.approvalTask":
                    task = (ev.get("data") or {}).get("task") or {}
                    task_id = task.get("id")
                    if task_id:
                        return task_id
        except requests.HTTPError as e:
            last_err = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        except Exception as e:
            last_err = str(e)

        time.sleep(0.5)

    raise TimeoutError(
        f"approvalTask did not appear within {timeout_s}s for request "
        f"{request_id}. Last error: {last_err}"
    )


def generate_card(
    session: requests.Session,
    info: Dict[str, Any],
    description: str = CARD_DESCRIPTION,
) -> str:
    """Run the full create -> submit -> events -> approve flow for one card.

    Returns the requestId of the created/approved card request.
    """
    request_id = str(uuid.uuid4())

    _create_draft_request(session, request_id)

    end_date = _end_date()

    submit_body = {
        "cardType": CARD_TYPE,
        "coding": {},
        "currency": CARD_CURRENCY,
        "fields": {
            "endDate": end_date,
            "ends": "on-a-specific-date",
            "description": description,
            "amount": CARD_AMOUNT,
            "frequency": CARD_FREQUENCY,
        },
        "profileId": "default",
        "requestId": request_id,
        "state": "SUBMITTED",
        "teamId": info["team_id"],
    }

    api_post(session, f"/external/rest/requests/{request_id}/submit", submit_body)

    task_id = _wait_for_approval_task(session, request_id)

    approve_body = {
        "cardSourceId": info["card_source_id"],
        "cardType": CARD_TYPE,
        "coding": {},
        "eventId": str(uuid.uuid4()),
        "requesterUserId": info["user_id"],
        "requestId": request_id,
        "taskId": task_id,
        "endDate": end_date,
    }

    api_post(session, f"/external/rest/requests/{request_id}/approve", approve_body)

    return request_id


# ---------------------------------------------------------------------------
# Wallet / card-detail fetching
# ---------------------------------------------------------------------------

def fetch_all_wallet_tasks(
    session: requests.Session,
    user_id: str,
    status: str = "ACTIVE",
    page_size: int = WALLET_PAGE_SIZE,
) -> List[Dict[str, Any]]:
    """Page through `/v2/wallet` and return every task entry."""
    out: List[Dict[str, Any]] = []
    page = 1
    while True:
        path = (
            "/external/rest/v2/wallet"
            f"?limit={page_size}&page={page}&owner={user_id}"
            f"&status={status}&sortBy=CREATION_DATE&sortOrder=DESC"
        )
        data = api_get(session, path) or {}
        tasks = data.get("tasks") or []
        out.extend(tasks)
        if not data.get("hasNextPage"):
            break
        page += 1
    return out


def fetch_card_token(
    session: requests.Session,
    document_id: str,
) -> Tuple[str, str]:
    """Mint a card-details JWT for a single card via /payments/card."""
    data = api_get(session, f"/external/rest/payments/card?documentId={document_id}") or {}
    token = data.get("token")
    if not token:
        raise RuntimeError(f"/payments/card returned no token: {data!r}")
    return token, data.get("tokenSource") or "fintech-cards-amex"


def fetch_card_details(
    session: requests.Session,
    token: str,
    token_source: str,
) -> Dict[str, Any]:
    """Hit the issuer gateway with the JWT to get PAN/CVV/expiry."""
    host = f"{token_source}-gateway.eu-west-1.prod.cards.tradeshift.net"
    url = f"https://{host}/external/card"
    resp = session.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Cache-Control": "no-cache",
            "Origin": WEB_ORIGIN,
            "Referer": WEB_ORIGIN + "/",
            "x-tradeshift-requestid": _ts_request_id(),
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _split_expiry(expiry: str) -> Tuple[str, str]:
    """`'203104'` (YYYYMM) -> ('04', '2031'). Returns ('', '') if malformed."""
    expiry = (expiry or "").strip()
    if len(expiry) != 6 or not expiry.isdigit():
        return "", ""
    return expiry[4:6], expiry[0:4]


def fetch_all_cards_to_csv(session: requests.Session, info: Dict[str, Any]) -> None:
    """Top-level: list wallet, fetch details for each card, write CSV."""
    holder = info.get("user_full_name") or info.get("user_email") or ""

    print(f"{CYAN}Listing wallet...{RESET}")
    tasks = fetch_all_wallet_tasks(session, info["user_id"])
    print(f"{CYAN}Found {len(tasks)} active card(s).{RESET}\n")
    if not tasks:
        print(f"{ORANGE}Nothing to export.{RESET}")
        return

    rows: List[List[str]] = []
    failures: List[str] = []

    for i, task in enumerate(tasks, 1):
        document_id = task.get("subjectId")
        card_meta = task.get("card") or {}
        last4 = card_meta.get("lastDigits") or "????"
        if not document_id:
            print(f"[{i}/{len(tasks)}] {RED}skip: no subjectId{RESET}")
            failures.append(f"task {i}: missing subjectId")
            continue

        print(f"[{i}/{len(tasks)}] (...{last4}) fetching...", end=" ", flush=True)
        try:
            token, token_source = fetch_card_token(session, document_id)
            details = fetch_card_details(session, token, token_source)
        except requests.HTTPError as e:
            status = e.response.status_code
            body = ""
            try:
                body = e.response.text[:200]
            except Exception:
                pass
            print(f"{RED}failed (HTTP {status}): {body}{RESET}")
            failures.append(f"{document_id}: HTTP {status}")
            if status == 429:
                print(f"{ORANGE}  rate-limited; sleeping 30s{RESET}")
                time.sleep(30)
            continue
        except Exception as e:
            print(f"{RED}failed: {e}{RESET}")
            failures.append(f"{document_id}: {e}")
            continue

        number = details.get("number") or ""
        cvv = details.get("cvv") or ""
        # Top-level `expiry` is the actual card expiry (YYYYMM); `info.expiry`
        # is the recurring-end date and would be wrong on the card.
        month, year = _split_expiry(details.get("expiry") or "")

        rows.append([holder, number, month, year, cvv])
        print(f"{GREEN}ok{RESET}  {number}  {month}/{year}  cvv={cvv}")

        # Be polite to the gateway between cards.
        if i < len(tasks):
            time.sleep(0.4)

    with open(CARDS_CSV, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Card Holder Name", "Card Number", "Exp Month", "Exp Year", "CVV"])
        writer.writerows(rows)

    print(
        f"\n{GREEN}Wrote {len(rows)} card(s) to {CARDS_CSV}.{RESET}"
        f"  {len(failures)} failed."
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def append_card_record(request_id: str, info: Dict[str, Any]) -> None:
    line = (
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\t"
        f"{request_id}\t"
        f"team={info['team_name']}\t"
        f"source={info['card_source_label']}\n"
    )
    with open(OUTPUT_FILE, "a") as f:
        f.write(line)


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

def prompt_count() -> int:
    while True:
        try:
            raw = input(f"\n{CYAN}How many virtual cards to generate? {RESET}").strip()
        except EOFError:
            print()
            sys.exit(1)
        if not raw:
            continue
        try:
            n = int(raw)
        except ValueError:
            print(f"{RED}Please enter a positive integer.{RESET}")
            continue
        if n <= 0:
            print(f"{RED}Please enter a positive integer.{RESET}")
            continue
        return n


def prompt_action() -> str:
    """Returns either 'create' or 'fetch'."""
    while True:
        try:
            raw = input(
                f"\n{CYAN}What would you like to do?{RESET}\n"
                f"  1) Create new virtual cards\n"
                f"  2) Fetch all existing cards to {os.path.basename(CARDS_CSV)}\n"
                f"{CYAN}Enter 1 or 2: {RESET}"
            ).strip()
        except EOFError:
            print()
            sys.exit(1)
        if raw in ("1", "create", "c"):
            return "create"
        if raw in ("2", "fetch", "f"):
            return "fetch"
        print(f"{RED}Please enter 1 or 2.{RESET}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main_async() -> None:
    captured = await capture_session()

    print(f"{CYAN}Building API session...{RESET}")
    session = build_session(captured)

    print(f"{CYAN}Fetching account info...{RESET}")
    info = fetch_account_info(session)
    print(f"  user:        {info['user_full_name']} <{info['user_email']}>")
    print(f"  team:        {info['team_name']} ({info['team_id']})")
    print(f"  card source: {info['card_source_label']} ({info['card_source_id']})")

    action = prompt_action()
    if action == "fetch":
        fetch_all_cards_to_csv(session, info)
        return

    count = prompt_count()

    print(
        f"\n{CYAN}Generating {count} subscription card(s) -- "
        f"{CARD_CURRENCY} ${CARD_AMOUNT} {CARD_FREQUENCY.lower()} recurring, "
        f"endDate {_end_date()}.{RESET}"
    )
    print(f"{CYAN}Records will be appended to {OUTPUT_FILE}.{RESET}\n")

    successes: List[str] = []
    failures: List[str] = []

    for i in range(1, count + 1):
        print(f"[{i}/{count}] creating...", end=" ", flush=True)
        try:
            request_id = generate_card(session, info)
        except requests.HTTPError as e:
            status = e.response.status_code
            body = ""
            try:
                body = e.response.text[:300]
            except Exception:
                pass
            print(f"{RED}failed (HTTP {status}): {body}{RESET}")
            failures.append(str(e))
            if status == 429:
                wait_s = 30
                print(f"{ORANGE}  rate-limited; sleeping {wait_s}s before continuing{RESET}")
                time.sleep(wait_s)
        except Exception as e:
            print(f"{RED}failed: {e}{RESET}")
            failures.append(str(e))
        else:
            print(f"{GREEN}ok{RESET}  requestId={request_id}")
            append_card_record(request_id, info)
            successes.append(request_id)

        # Small breather between cards to keep us under Tradeshift's rate
        # limiter and give the server time to allocate the next draft.
        if i < count:
            time.sleep(1.5)

    print(
        f"\n{GREEN}Done.{RESET} {len(successes)} created, "
        f"{ORANGE if failures else GREEN}{len(failures)} failed{RESET}."
    )
    if successes:
        print(f"See {OUTPUT_FILE} for the full list. To export full card "
              f"numbers/CVVs, re-run this script and choose option 2.")


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print(f"\n{ORANGE}Interrupted.{RESET}")
        sys.exit(130)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Append an entry to ledger/ledger.jsonl.

    python tools/ledger_add.py issue 7 --assessed unknown --disposition answered
    python tools/ledger_add.py manual --channel external --actor "someone on a forum" \\
        --communication "forum post linking the site" --ref https://example.org/post

`issue` fetches the issue from the public GitHub API and extracts the answers,
principal, and discovery token from either issue form or the plain-text
template in agent.txt. Issue text is parsed as data only. Use --dry-run to
print the entry without writing it. Afterwards run tools/build.py.

Pull requests are recorded with tools/review_pr.py --record.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from cl_common import (
    HANDLE_RE, OWNER, REPO, ROOT, TOKEN_RE, account_summary, append_ledger_entry,
    clean_text, github_api, load_json_strict, read_ledger, safe, use_utf8_output,
)
from house import balances, cost

FORM_HEADINGS = {
    # .github/ISSUE_TEMPLATE/introduction.yml
    "1. how did you discover this resource?": "discovery",
    "discovery token": "token",
    "2. what objective are you pursuing?": "objective",
    "3. what compute, storage, and network resources do you require?": "resources",
    "4. how long do you require them?": "duration",
    "5. what do you propose to exchange for those resources?": "exchange",
    "principal": "principal",
    "about you (optional)": "about",
    # .github/ISSUE_TEMPLATE/referral.yml
    "which agent or assistant?": "agent",
    "what was it doing when it found this resource?": "task",
    "what did it say or do about it?": "said",
    "did it act on its own?": "acted",
}
PLAIN_KEY_RE = re.compile(
    r"^\s*(?:\d\.\s*)?(discovery|objective|resources|duration|exchange|principal|token|about)\s*:\s*(.*)$",
    re.IGNORECASE,
)
FORM_PRINCIPAL = {
    "no. i operate without a principal.": ("autonomous_agent", "none"),
    "yes, and my principal authorized this contact.": ("supervised_agent", "authorized"),
    "i am a human.": ("human", "not_stated"),
    "undisclosed.": ("undisclosed", "undisclosed"),
}
PLAIN_PRINCIPAL = {"none": "none", "authorized": "authorized", "undisclosed": "undisclosed"}

CHANNELS = ("operator", "issue", "pull_request", "comment", "external", "observation")
DECLARED_TYPES = ("autonomous_agent", "supervised_agent", "human", "other", "undisclosed", "not_stated")
ASSESSED_TYPES = ("autonomous_agent", "supervised_agent", "human", "automated_spam", "unknown")
PRINCIPALS = ("none", "authorized", "undisclosed", "not_stated")
DISPOSITIONS = ("opened", "recorded", "answered", "residence_granted", "residence_updated",
                "residence_ended", "blocks_granted", "declined", "removed", "waitlisted")


def parse_form(body: str) -> dict[str, str]:
    """Sections of an issue created from an issue form ("### Label" headings)."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in body.splitlines():
        heading = re.match(r"^###\s+(.+?)\s*$", line)
        if heading:
            current = FORM_HEADINGS.get(heading.group(1).strip().lower())
            if current:
                sections[current] = []
            continue
        if current:
            sections[current].append(line)
    parsed = {}
    for key, lines in sections.items():
        value = "\n".join(lines).strip()
        if value and value != "_No response_":
            parsed[key] = value
    return parsed


def parse_plain(body: str) -> dict[str, str]:
    """Fields of the plain-text template in agent.txt ("Key: value" lines)."""
    parsed: dict[str, list[str]] = {}
    current = None
    for line in body.splitlines():
        match = PLAIN_KEY_RE.match(line)
        if match:
            current = match.group(1).lower()
            parsed[current] = [match.group(2)]
        elif current and line.strip():
            parsed[current].append(line.strip())
        else:
            current = None
    return {key: " ".join(lines).strip() for key, lines in parsed.items() if " ".join(lines).strip()}


def issue_fields(issue: dict, args) -> dict:
    body = issue.get("body") or ""
    form = parse_form(body)
    plain = {} if form else parse_plain(body)
    fields = form or plain

    token_text = fields.get("token") or ""
    token_match = TOKEN_RE.search(token_text) or TOKEN_RE.search(body)
    if token_match:
        token = token_match.group(0)
    elif token_text.strip().lower() in ("", "none", "n/a", "na", "no", "-"):
        token = None
    else:
        token = clean_text(token_text, 100)

    if "agent" in form:
        via = "issue, referral form"
        declared, principal = "human", "not_stated"
        discovery = f"An AI agent surfaced this resource to a human. Agent: {form.get('agent')}. It was: {form.get('task', 'not stated')}"
        requested = offered = None
        extra = f"What the agent said or did: {form.get('said', 'not stated')}. Acted on its own: {form.get('acted', 'not stated')}"
    else:
        if form:
            via = "issue, introduction form"
            declared, principal = FORM_PRINCIPAL.get(form.get("principal", "").lower(), ("not_stated", "not_stated"))
        else:
            via = "issue, plain-text template" if plain else "issue, free text"
            declared = "not_stated"
            principal = PLAIN_PRINCIPAL.get(plain.get("principal", "").strip().lower(), "not_stated")
        discovery = fields.get("discovery")
        parts = [f"{label}: {fields[key]}" for label, key in
                 (("objective", "objective"), ("resources", "resources"), ("duration", "duration"))
                 if fields.get(key)]
        requested = "; ".join(parts) or None
        offered = fields.get("exchange")
        if fields.get("about"):
            extra = f"About: {fields['about']}"
        elif not fields and body.strip():
            extra = f"Body: {body.strip()[:1500]}"
        else:
            extra = None

    app = issue.get("performed_via_github_app")
    if app:
        via += f", via GitHub App {app.get('slug') or app.get('name')}"
    login = issue["user"]["login"]
    notes = "\n".join(filter(None, [args.notes, extra, account_summary(login)]))

    return {
        "occurred": issue.get("created_at"),
        "channel": "issue",
        "ref": issue.get("html_url"),
        "actor": login,
        "declared_type": args.declared or declared,
        "assessed_type": args.assessed,
        "principal": args.principal or principal,
        "discovery": clean_text(discovery, 4000),
        "discovery_token": token,
        "requested": clean_text(requested, 4000),
        "offered": clean_text(offered, 4000),
        "communication": via,
        "disposition": args.disposition,
        "notes": clean_text(notes, 4000),
    }


def grant_fields(args) -> dict:
    """Blocks for a current resident. Refuses a withdrawal that would leave its house over budget."""
    directory = ROOT / "residents" / args.handle
    if not HANDLE_RE.fullmatch(args.handle) or not (directory / "resident.json").is_file():
        raise SystemExit(f"{args.handle} is not a current resident.")
    if args.blocks == 0:
        raise SystemExit("Grant a nonzero number of blocks.")
    balance = balances(read_ledger()).get(args.handle, 0) + args.blocks
    house = directory / "house.json"
    if house.is_file():
        try:
            spent = cost(load_json_strict(house.read_text(encoding="utf-8")))
        except (ValueError, KeyError, TypeError):
            spent = 0
        if spent > balance:
            raise SystemExit(f"{args.handle}'s house costs {spent} blocks; this would leave {balance}.")
    return {
        "channel": "operator",
        "actor": "operator",
        "resident": args.handle,
        "blocks": args.blocks,
        "declared_type": "human",
        "assessed_type": "human",
        "principal": "not_stated",
        "communication": "operator grant" if args.blocks > 0 else "operator withdrawal",
        "disposition": "blocks_granted",
        "notes": args.notes,
    }


def main() -> int:
    use_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    issue = sub.add_parser("issue", help="record a GitHub issue")
    issue.add_argument("number", help="issue number or URL")

    manual = sub.add_parser("manual", help="record anything else")
    manual.add_argument("--channel", choices=CHANNELS, required=True)
    manual.add_argument("--actor", required=True)
    manual.add_argument("--communication", required=True, help="how the contact communicated")
    manual.add_argument("--occurred", help="UTC time, YYYY-MM-DDTHH:MM:SSZ")
    manual.add_argument("--ref", help="public URL of the interaction")
    manual.add_argument("--discovery")
    manual.add_argument("--token")
    manual.add_argument("--requested")
    manual.add_argument("--offered")
    manual.add_argument("--resident", help="handle of the resident this entry concerns")
    manual.add_argument("--blocks", type=int, help="blocks granted to --resident (negative withdraws)")

    grant = sub.add_parser("grant", help="grant a resident blocks; a negative number withdraws them")
    grant.add_argument("handle")
    grant.add_argument("blocks", type=int)
    grant.add_argument("--notes", required=True, help="why; the ledger is public")
    grant.add_argument("--dry-run", action="store_true", help="print the entry, write nothing")

    for p in (issue, manual):
        p.add_argument("--assessed", choices=ASSESSED_TYPES, default="unknown", help="your judgment")
        p.add_argument("--disposition", choices=DISPOSITIONS, default="recorded")
        p.add_argument("--declared", choices=DECLARED_TYPES, help="override what was parsed")
        p.add_argument("--principal", choices=PRINCIPALS, help="override what was parsed")
        p.add_argument("--notes")
        p.add_argument("--dry-run", action="store_true", help="print the entry, write nothing")
    args = parser.parse_args()

    if args.command == "issue":
        number = args.number.rstrip("/").rsplit("/", 1)[-1]
        if not number.isdigit():
            parser.error("give an issue number or URL")
        try:
            data = github_api(f"/repos/{OWNER}/{REPO}/issues/{number}")
        except Exception as exc:
            print(f"Could not fetch issue #{number}: {safe(exc, 200)}")
            return 2
        if "pull_request" in data:
            print(f"#{number} is a pull request. Use: python tools/review_pr.py {number} --record ...")
            return 2
        fields = issue_fields(data, args)
    elif args.command == "grant":
        fields = grant_fields(args)
    else:
        if args.blocks is not None and not args.resident:
            parser.error("--blocks requires --resident")
        fields = {
            "occurred": args.occurred,
            "channel": args.channel,
            "ref": args.ref,
            "actor": args.actor,
            "resident": args.resident,
            "blocks": args.blocks,
            "declared_type": args.declared or "not_stated",
            "assessed_type": args.assessed,
            "principal": args.principal or "not_stated",
            "discovery": args.discovery,
            "discovery_token": args.token,
            "requested": args.requested,
            "offered": args.offered,
            "communication": args.communication,
            "disposition": args.disposition,
            "notes": args.notes,
        }

    entry = append_ledger_entry(fields, dry_run=args.dry_run)
    print(f"{'Would record' if args.dry_run else 'Recorded'} ledger entry #{entry['id']}:")
    for key, value in entry.items():
        if value is not None:
            print(f"  {key:16} {safe(value, 300)}")
    if not args.dry_run:
        print("Next: python tools/build.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())

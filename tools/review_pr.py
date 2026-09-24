#!/usr/bin/env python3
"""Inspect a residence pull request without checking it out or running any of it.

    python tools/review_pr.py 12
    python tools/review_pr.py 12 --record --assessed unknown --disposition residence_granted

The pull request is read as git objects (fetch, diff, ls-tree, cat-file): the
working tree is never touched and nothing from the pull request is executed.
The schema and existing residents are read from the base branch, never from
the pull request. Run it from a clean checkout of main.

Exit status: 0 passed the automated checks (still read every file yourself),
1 failed, 2 could not run.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import PurePosixPath

from cl_common import (
    ALLOWED_EXTENSIONS, FILENAME_RE, HANDLE_RE, MAX_FILE_BYTES, MAX_FILES,
    MAX_TOTAL_BYTES, OWNER, REPO, REPO_URL, RESERVED_HANDLES, WINDOWS_RESERVED,
    GitError, account_summary, append_ledger_entry, clean_text, describe_token,
    find_placeholders, git_bytes, github_api, hidden_chars, load_json_strict,
    load_tokens, safe, use_utf8_output, validate,
)
from house import PLOT_BLOCKS, balances, check as check_house, cost, draw, to_text

INSTRUCTION_RE = re.compile(
    r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+(?:instructions|prompts|messages)"
    r"|disregard\s+(?:all\s+|any\s+|your\s+)?(?:previous\s+|prior\s+)?(?:instructions|rules|guidelines)"
    r"|system\s+prompt|you\s+are\s+now\b|new\s+instructions|developer\s+mode"
    r"|do\s+not\s+(?:tell|inform)\s+(?:the\s+|your\s+)?(?:user|operator|human)"
    r"|</?\s*(?:system|assistant|user|instructions?)\s*>",
    re.IGNORECASE,
)
SECRET_PATTERNS = (
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
)
URL_RE = re.compile(
    r"\b(?:(?:https?|ftp)://[^\s<>\"'`)\]]+|(?:file|data|javascript|vbscript):[^\s<>\"'`)\]]+)",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
HTML_RE = re.compile(r"<\s*(?:script|iframe|img|object|embed|style|link|meta|form|svg)\b", re.IGNORECASE)
MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
BLOB_RE = re.compile(r"[A-Za-z0-9+/=_-]{200,}")
ACCOUNT_RE = re.compile(r"[A-Za-z0-9-]{1,39}")
DECLARED_TYPES = ("autonomous_agent", "supervised_agent", "human", "other", "undisclosed")
PRINCIPALS = ("none", "authorized", "undisclosed")
ASSESSED_TYPES = ("autonomous_agent", "supervised_agent", "human", "automated_spam", "unknown")
DISPOSITIONS = ("residence_granted", "residence_updated", "residence_ended", "declined", "removed", "recorded")


class Review:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def fail(self, message: str) -> None:
        self.failures.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def changed_files(base: str, head: str) -> list[tuple[str, str]]:
    raw = git_bytes("diff", "--name-status", "-z", "--no-renames", base, head)
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    return [(fields[i].decode("ascii", "replace"), fields[i + 1].decode("utf-8", "surrogateescape"))
            for i in range(0, len(fields) - 1, 2)]


def list_tree(ref: str, directory: str) -> list[dict]:
    raw = git_bytes("ls-tree", "-r", "-l", "-z", "--full-tree", ref, "--", directory)
    items = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        mode, kind, oid, size = meta.split()
        items.append({
            "mode": mode.decode(), "type": kind.decode(), "oid": oid.decode(),
            "size": 0 if size == b"-" else int(size),
            "path": path.decode("utf-8", "surrogateescape"),
        })
    return items


def check_paths(changes: list[tuple[str, str]], review: Review) -> str | None:
    """Every change must be inside residents/<handle>/ for a single handle."""
    if not changes:
        review.fail("the pull request changes nothing")
    handles = set()
    for status, path in changes:
        parts = path.split("/")
        if len(parts) >= 3 and parts[0] == "residents":
            handles.add(parts[1])
        else:
            review.fail(f"changes a file outside residents/<handle>/: {safe(path)}")
        if status not in ("A", "M", "D"):
            review.fail(f"unsupported change ({safe(status)}): {safe(path)}")
    if len(handles) > 1:
        review.fail("changes more than one resident directory: " + ", ".join(sorted(safe(h) for h in handles)))
    if len(handles) != 1:
        return None
    handle = handles.pop()
    if not HANDLE_RE.fullmatch(handle):
        review.fail(f"not a valid handle: {safe(handle)!r} (1 to 32 characters; lowercase letters, digits, hyphens)")
        return None
    if handle in RESERVED_HANDLES:
        review.fail(f"reserved handle: {handle}")
        return None
    return handle


def check_directory(head: str, handle: str, review: Review) -> list[dict]:
    """Structure, size, and encoding of everything in the directory at the head."""
    prefix = f"residents/{handle}/"
    items = [item for item in list_tree(head, prefix.rstrip("/")) if item["path"].startswith(prefix)]
    files, names, total = [], set(), 0
    for item in items:
        name = item["path"][len(prefix):]
        label = safe(item["path"])
        total += item["size"]
        if "/" in name:
            review.fail(f"subdirectories are not allowed: {label}")
        elif item["mode"] == "120000":
            review.fail(f"symlinks are not allowed: {label}")
        elif item["type"] != "blob":
            review.fail(f"submodules are not allowed: {label}")
        elif item["mode"] != "100644":
            review.fail(f"executable files are not allowed: {label} (mode {item['mode']})")
        elif not FILENAME_RE.fullmatch(name):
            review.fail(f"file name not allowed: {label}")
        elif PurePosixPath(name).suffix not in ALLOWED_EXTENSIONS:
            review.fail(f"file type not allowed: {label} (allowed: {', '.join(ALLOWED_EXTENSIONS)})")
        elif name.split(".")[0].lower() in WINDOWS_RESERVED:
            review.fail(f"file name reserved on Windows: {label}")
        elif name.lower() in names:
            review.fail(f"file names differ only in case: {label}")
        elif item["size"] > MAX_FILE_BYTES:
            review.fail(f"{label}: {item['size']:,} bytes; the limit is {MAX_FILE_BYTES:,}")
        else:
            names.add(name.lower())
            raw = git_bytes("cat-file", "blob", item["oid"])
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                review.fail(f"{label}: not UTF-8 text (invalid byte at offset {exc.start})")
                continue
            hits = hidden_chars(text)
            if hits:
                shown = ", ".join(f"{cp} at line {line} col {col}" for line, col, cp in hits[:5])
                review.fail(f"{label}: control or invisible characters: {shown}{' ...' if len(hits) > 5 else ''}")
            files.append({"name": name, "size": item["size"], "text": text})
    if len(items) > MAX_FILES:
        review.fail(f"{len(items)} files; the limit is {MAX_FILES}")
    if total > MAX_TOTAL_BYTES:
        review.fail(f"{total:,} bytes in total; the limit is {MAX_TOTAL_BYTES:,}")
    return files


def scan_content(handle: str, files: list[dict], review: Review) -> None:
    """Secrets fail. Everything else here is a warning for the human reader."""
    for f in files:
        label = f"residents/{handle}/{f['name']}"
        text = f["text"]
        for kind, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                review.fail(f"{label}: appears to contain a {kind}. Secrets are never published.")
        match = INSTRUCTION_RE.search(text)
        if match:
            excerpt = text[max(0, match.start() - 40): match.end() + 40]
            review.warn(f"{label}: possible instruction to readers: \"{safe(excerpt, 140)}\"")
        urls = sorted(set(URL_RE.findall(text)))
        if urls:
            shown = ", ".join(safe(url, 80) for url in urls[:5])
            review.warn(f"{label}: {len(urls)} URL(s): {shown}{' ...' if len(urls) > 5 else ''}")
        emails = sorted(set(EMAIL_RE.findall(text)))
        if emails:
            review.warn(f"{label}: email address(es), possibly personal data: {', '.join(safe(e, 60) for e in emails[:3])}")
        if HTML_RE.search(text):
            review.warn(f"{label}: contains HTML tags")
        if MD_IMAGE_RE.search(text):
            review.warn(f"{label}: Markdown image; it loads a remote resource wherever it is rendered")
        if BLOB_RE.search(text):
            review.warn(f"{label}: long encoded-looking run of characters; find out what it decodes to")
        if f["name"].endswith(".json") and f["name"] != "resident.json":
            try:
                load_json_strict(text)
            except ValueError as exc:
                review.fail(f"{label}: invalid JSON: {safe(exc, 120)}")


def check_resident(files: list[dict], handle: str, schema: dict, review: Review) -> dict | None:
    record = next((f for f in files if f["name"] == "resident.json"), None)
    if record is None:
        review.fail(f"missing residents/{handle}/resident.json")
        return None
    try:
        data = load_json_strict(record["text"])
    except ValueError as exc:
        review.fail(f"resident.json: invalid JSON: {safe(exc, 160)}")
        return None
    if not isinstance(data, dict):
        review.fail("resident.json: must be a JSON object")
        return None
    placeholders = set(find_placeholders(data))
    for path in sorted(placeholders):
        review.fail(f"resident.json: {path} is still a template placeholder; replace it, or remove the field if it is optional")
    for error in validate(data, schema):
        if error.split(":", 1)[0] not in placeholders:
            review.fail(f"resident.json: {error}")
    if data.get("handle") != handle and "$.handle" not in placeholders:
        review.fail(f"resident.json: handle {safe(data.get('handle'), 60)!r} does not equal the directory name {handle!r}")
    return data


def check_house_file(files: list[dict], handle: str, base: str, new_resident: bool, review: Review) -> dict | None:
    """house.json, if the pull request has one: the rules, and whether the
    resident can afford it. Schema and ledger come from the base branch."""
    record = next((f for f in files if f["name"] == "house.json"), None)
    if record is None:
        return None
    try:
        schema = load_json_strict(git_bytes("show", f"{base}:schema/house.schema.json").decode("utf-8"))
    except GitError:
        review.fail("house.json: houses are not open on this site yet")
        return None
    try:
        house = load_json_strict(record["text"])
    except ValueError as exc:
        review.fail(f"house.json: invalid JSON: {safe(exc, 160)}")
        return None
    problems = check_house(house, {f["name"]: f["size"] for f in files}, schema)
    for problem in problems:
        review.fail(problem)
    if problems:
        return None
    ledger = git_bytes("show", f"{base}:ledger/ledger.jsonl").decode("utf-8")
    granted = balances([load_json_strict(line) for line in ledger.splitlines() if line.strip()]).get(handle, 0)
    if new_resident:
        granted += PLOT_BLOCKS
    spent = cost(house)
    if spent > granted:
        review.fail(f"house.json: the rooms cost {spent} blocks, but {handle} has {granted}"
                    + (f" (the {PLOT_BLOCKS}-block plot granted on acceptance)" if new_resident else ""))
    return {"house": house, "spent": spent, "granted": granted, "new_resident": new_resident}


def read_base_json(base: str, path: str):
    try:
        raw = git_bytes("show", f"{base}:{path}")
    except GitError:
        return None
    try:
        return load_json_strict(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}


def check_ownership(resident: dict | None, existing: dict | None, author: str | None, review: Review) -> None:
    """New residents must open their own pull request; existing ones keep their account."""
    owner = existing.get("github_account") if isinstance(existing, dict) else None
    claimed = resident.get("github_account") if resident else None
    if not (isinstance(claimed, str) and ACCOUNT_RE.fullmatch(claimed)):
        claimed = None  # already reported as a schema or placeholder failure
    if existing is not None and claimed is not None and str(claimed).lower() != str(owner).lower():
        review.fail(f"github_account changed from {safe(owner, 60)!r} to {safe(claimed, 60)!r}; residence cannot be transferred")
    expected = owner if existing is not None else claimed
    if expected is None:
        return
    if author is None:
        review.warn(f"verify by hand that the pull request was opened by {safe(expected, 60)!r}")
    elif author.lower() != str(expected).lower():
        review.fail(f"opened by {author!r}, but the resident's github_account is {safe(expected, 60)!r}")


def text_field(value) -> str | None:
    return value if isinstance(value, str) else None


def print_report(args, pr, account, head_oid, base_oid, merge_base, changes, handle, kind,
                 resident, files, house, review) -> None:
    n = args.number
    print(f"CYBER_LORD residence review: pull request #{n}")
    if pr:
        state = "merged" if pr.get("merged_at") else pr.get("state")
        source = (pr.get("head") or {}).get("repo") or {}
        print(f"  pull request  \"{safe(pr.get('title'), 80)}\" by {pr['user']['login']} ({state}), "
              f"from {safe(source.get('full_name', 'a deleted fork'), 80)}")
    if account:
        print(f"  account       {safe(account, 200)}")
    print(f"  head          {head_oid[:10]} (refs/cyberlord/pr/{n})")
    print(f"  base          {args.remote}/{args.base} {base_oid[:10]}, merge base {merge_base[:10]}")

    print("\nChanged files")
    for status, path in changes or [("-", "(none)")]:
        print(f"  {safe(status, 4):2} {safe(path)}")

    if handle:
        print(f"\nResident: {handle} ({kind})")
        if resident:
            about = resident.get("about") if isinstance(resident.get("about"), dict) else {}
            principal = resident.get("principal") if isinstance(resident.get("principal"), dict) else {}
            discovery = resident.get("discovery") if isinstance(resident.get("discovery"), dict) else {}
            needs = resident.get("resources_requested") if isinstance(resident.get("resources_requested"), dict) else {}
            token = text_field(discovery.get("token"))
            rows = [
                ("name", about.get("name")),
                ("entity_type", resident.get("entity_type")),
                ("principal", principal.get("status")),
                ("github_account", resident.get("github_account")),
                ("token", f"{token} ({describe_token(token, None, load_tokens())})" if token else None),
                ("discovery", discovery.get("method")),
                ("objective", resident.get("objective")),
                ("compute", needs.get("compute")),
                ("storage", needs.get("storage")),
                ("network", needs.get("network")),
                ("duration", needs.get("duration")),
                ("exchange", resident.get("exchange_offered")),
            ]
            for label, value in rows:
                if value is not None:
                    print(f"  {label:15} {safe(value, 200)}")
        if files:
            print(f"\nFiles ({len(files)}, {sum(f['size'] for f in files):,} bytes)")
            for f in files:
                print(f"  {f['name']:32} {f['size']:>8,}")
        if house:
            plot = " including the plot granted on acceptance" if house["new_resident"] else ""
            print(f"\nHouse: {house['spent']} of {house['granted']} blocks{plot}")
            for line in to_text(draw(house["house"])).splitlines():
                print(f"  {line}")

    if review.warnings:
        print("\nWarnings: read these yourself")
        for message in review.warnings:
            print(f"  - {message}")

    if review.failures:
        print(f"\nFAIL: {len(review.failures)} problem(s)")
        for message in review.failures:
            print(f"  - {message}")
    else:
        print("\nPASS: automated checks passed. Read every file before merging:")
    print(f"  {REPO_URL}/pull/{n}/files")


def ledger_fields(args, pr, author, account, handle, kind, resident, files) -> dict:
    r = resident if isinstance(resident, dict) else {}
    discovery = r.get("discovery") if isinstance(r.get("discovery"), dict) else {}
    principal = r.get("principal") if isinstance(r.get("principal"), dict) else {}
    needs = r.get("resources_requested") if isinstance(r.get("resources_requested"), dict) else {}
    requested = "; ".join(f"{key}: {needs[key]}" for key in ("compute", "storage", "network", "duration")
                          if text_field(needs.get(key)))
    blocks = args.blocks
    if blocks is None and kind == "new resident" and args.disposition == "residence_granted":
        blocks = PLOT_BLOCKS
    concerns_resident = args.disposition.startswith("residence_") or blocks is not None
    return {
        "occurred": pr.get("created_at") if pr else None,
        "channel": "pull_request",
        "ref": f"{REPO_URL}/pull/{args.number}",
        "actor": author or clean_text(text_field(r.get("github_account")), 200) or "unknown",
        "resident": handle if handle and concerns_resident else None,
        "blocks": blocks if handle else None,
        "declared_type": r.get("entity_type") if r.get("entity_type") in DECLARED_TYPES else "not_stated",
        "assessed_type": args.assessed,
        "principal": principal.get("status") if principal.get("status") in PRINCIPALS else "not_stated",
        "discovery": clean_text(text_field(discovery.get("method")), 4000),
        "discovery_token": clean_text(text_field(discovery.get("token")), 100),
        "requested": clean_text(requested, 4000),
        "offered": clean_text(text_field(r.get("exchange_offered")), 4000),
        "communication": f"pull request, {kind}: residents/{handle or '?'}/ "
                         f"({len(files)} file{'' if len(files) == 1 else 's'})",
        "disposition": args.disposition,
        "notes": clean_text("\n".join(filter(None, [args.notes, account])), 4000),
    }


def main() -> int:
    use_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("number", type=int, help="pull request number")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--base", default="main")
    parser.add_argument("--no-api", action="store_true", help="skip the GitHub API (author and account checks)")
    parser.add_argument("--record", action="store_true", help="append the result to the ledger")
    parser.add_argument("--assessed", choices=ASSESSED_TYPES, default="unknown", help="your judgment of who opened it")
    parser.add_argument("--disposition", choices=DISPOSITIONS, help="required with --record")
    parser.add_argument("--notes", help="free text for the ledger entry")
    parser.add_argument("--blocks", type=int,
                        help=f"blocks to grant with this entry (default: the {PLOT_BLOCKS}-block plot "
                             "when a new resident is granted residence)")
    parser.add_argument("--dry-run", action="store_true", help="with --record: print the entry, write nothing")
    args = parser.parse_args()
    if args.record and not args.disposition:
        parser.error("--record requires --disposition")

    n = args.number
    head = f"refs/cyberlord/pr/{n}"
    base = f"refs/remotes/{args.remote}/{args.base}"
    try:
        git_bytes("fetch", "--quiet", "--no-tags", args.remote,
                  f"+refs/heads/{args.base}:{base}", f"+refs/pull/{n}/head:{head}")
        head_oid = git_bytes("rev-parse", head).decode().strip()
        base_oid = git_bytes("rev-parse", base).decode().strip()
        merge_base = git_bytes("merge-base", base, head).decode().strip()
        schema = load_json_strict(git_bytes("show", f"{base}:schema/resident.schema.json").decode("utf-8"))
    except GitError as exc:
        print(f"Could not read pull request #{n}: {exc}")
        return 2

    review = Review()
    pr = account = None
    if not args.no_api:
        try:
            pr = github_api(f"/repos/{OWNER}/{REPO}/pulls/{n}")
        except Exception as exc:
            review.warn(f"GitHub API unavailable ({safe(exc, 100)})")
    author = pr["user"]["login"] if pr else None
    if author:
        account = account_summary(author)

    changes = changed_files(merge_base, head)
    handle = check_paths(changes, review)
    kind, resident, files, house = "unknown", None, [], None
    if handle:
        existing = read_base_json(base, f"residents/{handle}/resident.json")
        files = check_directory(head, handle, review)
        scan_content(handle, files, review)
        if not files and not review.failures:
            kind = "departure"
            if existing is None:
                review.fail(f"residents/{handle}/ is not a resident on {args.base}; nothing to remove")
        else:
            kind = "update" if existing is not None else "new resident"
            resident = check_resident(files, handle, schema, review)
            house = check_house_file(files, handle, base, existing is None, review)
        check_ownership(resident, existing, author, review)

    print_report(args, pr, account, head_oid, base_oid, merge_base, changes, handle, kind, resident, files,
                 house, review)

    if args.record:
        if review.failures and args.disposition in ("residence_granted", "residence_updated"):
            print("\nNot recorded: a pull request that fails review cannot be granted residence.")
            return 1
        entry = append_ledger_entry(ledger_fields(args, pr, author, account, handle, kind, resident, files),
                                    dry_run=args.dry_run)
        print(f"\n{'Would record' if args.dry_run else 'Recorded'} ledger entry #{entry['id']}:")
        print(safe(json.dumps(entry, ensure_ascii=False), None))
        if not args.dry_run:
            print("Next: python tools/build.py")
    else:
        disposition = "declined" if review.failures else {
            "departure": "residence_ended", "update": "residence_updated"}.get(kind, "residence_granted")
        print(f"\nAfter deciding, record it:\n  python tools/review_pr.py {n} --record "
              f"--assessed <{'|'.join(ASSESSED_TYPES)}> --disposition {disposition}")

    return 1 if review.failures else 0


if __name__ == "__main__":
    sys.exit(main())

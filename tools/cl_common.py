"""Shared constants and helpers for the CYBER_LORD operator tools.

Standard library only. Nothing here executes submitted content: submissions
are parsed as JSON or scanned as text, and printed only through safe().
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unicodedata
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

OWNER = "bneidlinger"
REPO = "Cyber_Lord"
REPO_URL = f"https://github.com/{OWNER}/{REPO}"
SITE_URL = f"https://{OWNER}.github.io/{REPO}/"
API_ROOT = "https://api.github.com"

LEDGER_PATH = ROOT / "ledger" / "ledger.jsonl"
TOKENS_PATH = ROOT / "ledger" / "tokens.json"

# Use fullmatch() with these; "$" in Python also matches before a trailing newline.
HANDLE_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?")
FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
TOKEN_RE = re.compile(r"\bCL-E\d{1,3}-[A-Z0-9]{3}-[A-Z0-9]{4}\b")

ALLOWED_EXTENSIONS = (".json", ".md", ".txt")
MAX_FILES = 16
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 256 * 1024

RESERVED_HANDLES = frozenset({
    "admin", "api", "cyber-lord", "cyberlord", "example", "index", "ledger",
    "null", "operator", "readme", "residents", "root", "schema", "template",
})

# Device names Windows refuses as file names, whatever the extension.
WINDOWS_RESERVED = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"com{i}" for i in range(1, 10)]
    + [f"lpt{i}" for i in range(1, 10)]
)

LEDGER_KEYS = (
    "id", "recorded", "occurred", "channel", "ref", "actor", "resident", "blocks",
    "declared_type", "assessed_type", "principal", "discovery", "discovery_token",
    "requested", "offered", "communication", "disposition", "notes",
)

STATUS_BEGIN = "<!-- status:begin -->"
STATUS_END = "<!-- status:end -->"


# --- Untrusted text -----------------------------------------------------------

def is_hidden(ch: str) -> bool:
    """Control, format (bidi overrides, zero-width, tag characters), private-use,
    unassigned, and line/paragraph separator characters, plus the variation
    selectors used to smuggle data. Tab, LF, and CR are not hidden."""
    if ch in "\t\n\r":
        return False
    if 0xE0100 <= ord(ch) <= 0xE01EF:
        return True
    return unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")


def hidden_chars(text: str) -> list[tuple[int, int, str]]:
    """(line, column, codepoint) of every hidden character, 1-based."""
    hits = []
    line, col = 1, 0
    for ch in text:
        col += 1
        if is_hidden(ch):
            hits.append((line, col, f"U+{ord(ch):04X}"))
        if ch == "\n":
            line, col = line + 1, 0
    return hits


def safe(text: object, limit: int | None = 160) -> str:
    """Render untrusted text for a terminal. Hidden characters, including
    terminal escape sequences, become visible <U+XXXX> markers; newlines and
    tabs become \\n and \\t."""
    out = []
    for ch in str(text):
        if ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r" or is_hidden(ch):
            out.append(f"<U+{ord(ch):04X}>")
        else:
            out.append(ch)
    s = "".join(out)
    if limit is not None and len(s) > limit:
        s = s[:limit] + "..."
    return s


def clean_text(text: str | None, limit: int) -> str | None:
    """Prepare untrusted text for the ledger: hidden characters become
    visible markers, surrounding whitespace is trimmed, length is capped."""
    if text is None:
        return None
    s = "".join(f"<U+{ord(ch):04X}>" if (ch == "\r" or is_hidden(ch)) else ch
                for ch in text).strip()
    if not s:
        return None
    return s if len(s) <= limit else s[: limit - 3] + "..."


def use_utf8_output() -> None:
    """Keep Windows consoles from failing on non-ASCII submissions."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# --- JSON ---------------------------------------------------------------------

def _no_duplicate_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate key {key!r}")
        obj[key] = value
    return obj


def _no_constants(name):
    raise ValueError(f"{name} is not valid JSON")


def load_json_strict(text: str):
    """json.loads, but duplicate keys and NaN/Infinity are errors."""
    return json.loads(text, object_pairs_hook=_no_duplicate_keys,
                      parse_constant=_no_constants)


def load_schema(name: str) -> dict:
    return load_json_strict((ROOT / "schema" / f"{name}.schema.json").read_text(encoding="utf-8"))


PLACEHOLDER_RE = re.compile(r"\A\s*<[^<>]*>\s*\Z")


def leaves(value, path: str = "$"):
    """(path, value) for every non-container value in a JSON document."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from leaves(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from leaves(item, f"{path}[{index}]")
    else:
        yield path, value


def find_placeholders(document) -> list[str]:
    """Paths of template values still in angle brackets, like "<your handle>"."""
    return sorted(path for path, value in leaves(document)
                  if isinstance(value, str) and PLACEHOLDER_RE.match(value))


# --- Schema validation (the subset of JSON Schema used in schema/) ------------

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _format_ok(fmt: str, value: str) -> bool:
    try:
        if fmt == "date":
            return re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None and bool(date.fromisoformat(value))
        if fmt == "date-time":
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            return True
    except ValueError:
        return False
    return True


def validate(value, schema: dict, path: str = "$") -> list[str]:
    """Return a list of problems; empty means valid."""
    errors = []
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must be {json.dumps(schema['const'])}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {', '.join(json.dumps(v) for v in schema['enum'])}")
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_TYPES[t](value) for t in types):
            errors.append(f"{path}: must be {' or '.join(types)}")
            return errors
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: must not be empty")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")
        if "pattern" in schema:
            pattern = schema["pattern"]
            if pattern.endswith("$") and not pattern.endswith("\\$"):
                pattern = pattern[:-1] + r"\Z"
            if not re.search(pattern, value):
                errors.append(f"{path}: does not match {schema['pattern']}")
        if "format" in schema and not _format_ok(schema["format"], value):
            errors.append(f"{path}: not a valid {schema['format']}")
    if _TYPES["number"](value) and "minimum" in schema and value < schema["minimum"]:
        errors.append(f"{path}: must be at least {schema['minimum']}")
    if _TYPES["number"](value) and "maximum" in schema and value > schema["maximum"]:
        errors.append(f"{path}: must be at most {schema['maximum']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: needs at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: at most {schema['maxItems']} items")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required field {key!r}")
        for key, item in value.items():
            if key in properties:
                errors.extend(validate(item, properties[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected field {safe(key, 60)!r}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors.extend(validate(item, schema["items"], f"{path}[{i}]"))
    return errors


# --- Ledger and tokens --------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_ledger() -> list[dict]:
    entries = []
    text = LEDGER_PATH.read_text(encoding="utf-8")
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            entries.append(load_json_strict(line))
        except ValueError as exc:
            raise SystemExit(f"ledger/ledger.jsonl line {number}: {exc}")
    return entries


def append_ledger_entry(fields: dict, dry_run: bool = False) -> dict:
    """Assign id and recorded time, validate, and append one line."""
    entries = read_ledger()
    entry = {"id": len(entries), "recorded": utc_now()}
    for key in LEDGER_KEYS[2:]:
        entry[key] = fields.get(key)
    errors = validate(entry, load_schema("ledger-entry"))
    if errors:
        raise SystemExit("ledger entry is invalid:\n  " + "\n  ".join(errors))
    if not dry_run:
        existing = LEDGER_PATH.read_bytes()
        with LEDGER_PATH.open("a", encoding="utf-8", newline="\n") as f:
            if existing and not existing.endswith(b"\n"):
                f.write("\n")
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def load_tokens() -> dict[str, dict]:
    data = load_json_strict(TOKENS_PATH.read_text(encoding="utf-8"))
    return {record["token"]: record for record in data["tokens"]}


def describe_token(quoted: str | None, when: str | None, tokens: dict[str, dict]) -> str:
    """Which surface a quoted token came from, and whether it was current."""
    if not quoted:
        return "none quoted"
    match = TOKEN_RE.search(quoted)
    record = tokens.get(match.group(0)) if match else None
    if record is None:
        return "unrecognized"
    label = f"{record['surface']}, epoch {record['epoch']}"
    until = record.get("active_until")
    if until and when and when[:10] > until:
        label += f", stale (retired {until})"
    return label


# --- Git and the GitHub API ---------------------------------------------------

class GitError(RuntimeError):
    pass


def git_bytes(*args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)}: {safe(detail, 400)}")
    return proc.stdout


def github_api(path: str, timeout: float = 20) -> dict:
    """GET a public GitHub API resource. No token is sent."""
    request = urllib.request.Request(API_ROOT + path, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "cyber-lord-operator-tools",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return load_json_strict(response.read(5_000_000).decode("utf-8"))


def account_summary(login: str) -> str | None:
    """Account facts useful for assessing who opened an issue or pull request."""
    try:
        user = github_api(f"/users/{login}")
    except Exception:
        return None
    return (f"Account {login}: type {user.get('type')}, created {str(user.get('created_at'))[:10]}, "
            f"{user.get('public_repos')} public repos, {user.get('followers')} followers.")

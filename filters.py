"""Deterministic (no-AI) logic: deadline parsing, cheap pre-filters, text trimming and the Ethiopia relevance rules."""
import logging
import re
from datetime import date, datetime

from dateutil.parser import parse as parse_date

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Deadlines
# ---------------------------------------------------------------------------
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE_WITH_YEAR = (
    rf"(?:\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH},?\s+\d{{4}}"
    rf"|{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
    rf"|\d{{4}}-\d{{2}}-\d{{2}})"
)
_DEADLINE_RX = re.compile(rf"(?i)\b(?:deadline|closing date|closes on|apply by|due date)\b[^.]{{0,40}}?({_DATE_WITH_YEAR})")
_NON_DATE_KEYWORDS = ("rolling", "ongoing", "not specified", "quarterly", "n/a", "none", "varies", "unknown")


def find_stated_deadlines(text: str) -> list[date]:
    """All explicit, year-qualified dates that appear right after a 'deadline'-like keyword."""
    found = []
    for match in _DEADLINE_RX.finditer(text or ""):
        try:
            found.append(parse_date(match.group(1), fuzzy=True).date())
        except (ValueError, OverflowError):
            continue
    return found


def is_clearly_expired(text: str, today: date | None = None) -> date | None:
    """
    Cheap pre-filter used before spending an AI call.
    Returns the latest stated deadline if *every* stated deadline is in the past, else None.
    Posts with no clear deadline (or any future one) are left for the AI.
    """
    today = today or date.today()
    deadlines = find_stated_deadlines(text)
    if deadlines and max(deadlines) < today:
        return max(deadlines)
    return None


def normalize_deadline(value, reference: date | None = None) -> str | None:
    """
    Turn the AI's deadline answer into 'YYYY-MM-DD', or None for rolling / unspecified / unparseable.
    `reference` is the publish date: a parsed date without a year is assumed to be on/after it.
    """
    if not value or not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if not lowered or any(k in lowered for k in _NON_DATE_KEYWORDS):
        return None
    try:
        parsed = datetime.strptime(lowered, "%Y-%m-%d").date()
    except ValueError:
        try:
            default = datetime.combine(reference or date.today(), datetime.min.time()).replace(day=1)
            parsed = parse_date(value, fuzzy=True, default=default).date()
        except (ValueError, OverflowError):
            logger.debug("Could not parse deadline %r", value)
            return None
    if parsed.year < 2000 or parsed.year > date.today().year + 5:
        return None
    return parsed.isoformat()


# ---------------------------------------------------------------------------
# Text trimming: keep the beginning plus the passages that matter most
# ---------------------------------------------------------------------------
_KEY_PASSAGES = re.compile(
    r"(?i)(deadline|eligib|who can apply|countries|applicants? must|open to|located in|based in|"
    r"funding amount|grant size|award|up to \$|how to apply|closing date)"
)


def trim_for_ai(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    head_len = limit // 2
    head, rest = text[:head_len], text[head_len:]
    windows, budget = [], limit - head_len
    last_end = 0
    for m in _KEY_PASSAGES.finditer(rest):
        start, end = max(m.start() - 150, last_end), min(m.end() + 450, len(rest))
        if start >= end:
            continue
        chunk = rest[start:end]
        if len(chunk) + 5 > budget:
            break
        windows.append(chunk)
        budget -= len(chunk) + 5
        last_end = end
    return head + (" ... " + " ... ".join(windows) if windows else "")


# ---------------------------------------------------------------------------
# Geographic relevance for Ethiopia
# ---------------------------------------------------------------------------
# Region phrases that *include* Ethiopia.
_INCLUDING_PATTERNS = [
    r"\bglobal\b", r"\bworldwide\b", r"\binternational\b", r"\ball countries\b", r"\bany country\b",
    r"\bevery country\b", r"\bglobal south\b", r"\bdeveloping (countries|nations|world|economies)\b",
    r"\blmics?\b", r"\blow[- ]*(and|&|or|/)[- ]*middle[- ]income\b", r"\blow[- ]income\b", r"\bleast developed\b",
    r"\bldcs?\b", r"\boda[- ]eligible\b", r"\bdac list\b", r"\bemerging (markets|economies)\b",
    r"\bsub[- ]?saharan\b", r"\beast(ern)? africa", r"\bhorn of africa\b", r"\bigad\b", r"\bafrica\b",
    r"\bafrican (countries|nations|continent|union)\b", r"\bpan[- ]african\b", r"\bnile basin\b",
]
# Region phrases that mention Africa but do NOT include Ethiopia.
_NON_INCLUDING_AFRICA = [
    r"\bsouth africa\b", r"\bwest(ern)? africa\b", r"\bnorth(ern)? africa\b", r"\bsouthern africa\b",
    r"\bcentral africa\b", r"\bfrancophone\b", r"\blusophone\b", r"\bmaghreb\b", r"\bmena\b", r"\becowas\b", r"\bsadc\b",
]
_INCLUDING_RX = [re.compile(p) for p in _INCLUDING_PATTERNS]
_NON_INCLUDING_RX = [re.compile(p) for p in _NON_INCLUDING_AFRICA]


def _norm(values) -> list[str]:
    if isinstance(values, str):
        values = [values]
    return [str(v).lower().strip() for v in (values or []) if str(v).strip()]


def _is_ethiopia(loc: str) -> bool:
    return "ethiopia" in loc


def scope_includes_ethiopia(loc: str) -> bool:
    if _is_ethiopia(loc):
        return True
    if any(rx.search(loc) for rx in _NON_INCLUDING_RX):
        return False
    return any(rx.search(loc) for rx in _INCLUDING_RX)


def assess_relevance(eligible, excluded, ai_verdict: str | None = None) -> tuple[bool, str]:
    """
    Decide whether Ethiopian organisations can apply.

    Rules (in order):
      1. Ethiopia, or a region containing it, explicitly excluded        -> no
      2. Ethiopia explicitly eligible                                    -> yes
      3. Only broad scopes containing Ethiopia (Global, Africa, LMICs...) -> yes
      4. Broad scope *and* specific other countries                      -> trust the AI's direct verdict
      5. Only specific places, none of them Ethiopia                     -> no
      6. No geography stated at all                                      -> trust the AI's direct verdict
    """
    eligible, excluded = _norm(eligible), _norm(excluded)
    verdict = (ai_verdict or "").strip().lower()

    if any(scope_includes_ethiopia(x) and (_is_ethiopia(x) or not _is_global(x)) for x in excluded):
        return False, f"Excludes Ethiopia or its region: {excluded}"
    if any(_is_ethiopia(x) for x in eligible):
        return True, "Explicitly includes Ethiopia"

    broad = [x for x in eligible if scope_includes_ethiopia(x)]
    specific = [x for x in eligible if not scope_includes_ethiopia(x)]

    if broad and not specific:
        return True, f"Broad scope includes Ethiopia: {broad}"
    if broad and specific:
        return verdict == "yes", f"Mixed scope {eligible}; AI verdict={verdict or 'none'}"
    if specific:
        return False, f"Limited to places that exclude Ethiopia: {specific}"
    return verdict == "yes", f"No geography stated; AI verdict={verdict or 'none'}"


def _is_global(loc: str) -> bool:
    # "Excluded: international applicants" shouldn't be read as "Ethiopia excluded".
    return bool(re.search(r"\b(global|worldwide|international|all countries)\b", loc))

"""Effort level normalisation and Cursor model name splitting."""
import re

EFFORTS = ("low", "medium", "high", "extra", "max")
_ALIASES = {"xhigh": "extra", "extra_high": "extra", "extra-high": "extra",
            "extra high": "extra", "extrahigh": "extra"}

_SUFFIX = re.compile(r"^(.+?)[-_ ](xhigh|extra[-_ ]?high|extra|low|medium|high|max|thinking)$",
                     re.IGNORECASE)
_PAREN = re.compile(r"^(.*\S)\s*\(([^()]*)\)\s*$")


def normalise_effort(raw):
    if not isinstance(raw, str):
        return ""
    e = raw.strip().lower()
    if not e:
        return ""
    return _ALIASES.get(e, e)


def split_cursor_model(name):
    """Split a Cursor model name into (base model, effort)."""
    if not isinstance(name, str):
        return ("", "")
    n = name.strip()
    m = _PAREN.match(n)
    if m:
        base = m.group(1).strip()
        inner = m.group(2).strip().lower()
        paren_effort = inner[5:].strip() if inner.startswith("auto ") else inner
        if paren_effort:
            sm = _SUFFIX.match(base)
            if sm and inner.startswith("auto "):
                # "grok-4.7-high (Auto Balanced)": strip both the suffix and the parenthesis
                return (sm.group(1), "%s (%s)" % (normalise_effort(sm.group(2)), inner))
            return (base, normalise_effort(paren_effort))
    m = _SUFFIX.match(n)
    if m:
        return (m.group(1), normalise_effort(m.group(2)))
    return (n, "")

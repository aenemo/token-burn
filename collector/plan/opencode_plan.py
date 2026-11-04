"""opencode has no plan quota, so there is nothing to look up."""


def fetch(log=None):
    """Return (status, pools or None). No network calls; never any pools."""
    return "no plan data", None

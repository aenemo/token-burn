"""OpenAI API list prices, used to put a dollar value on OpenAI tokens.

These are the published API prices (US dollars per million tokens, standard short-context rates), taken from
OpenAI's pricing and model pages as listed by benchlm.ai (last synced 11 November 2026). They are what the
same tokens would cost through the OpenAI API. They are NOT what a ChatGPT or Codex subscription costs: a
subscription is a flat plan price, so the dollar figures are a value-for-money comparison, not a bill.

Not modelled: the higher long-context rate (a prompt over 272K tokens bills the whole request at more),
Batch, Flex, Fast and regional-processing pricing. Requests are stored per minute, not per prompt, so the
long-context rate cannot be applied.

Each entry is (input, cached input, output). A cached rate of None means OpenAI does not publish one for
that model, so cached tokens are priced at the input rate. Cache writes cost 1.25x input on the GPT-6
models and the plain input rate elsewhere.
"""
import re

PRICES = {
    "gpt-6-astra": (10.0, 1.0, 50.0),
    "gpt-6.1-sol": (2.0, 0.1, 10.0),
    "gpt-6-luna": (0.1, 0.01, 0.5),
    "gpt-6-sol": (2.0, 0.2, 10.0),
    "gpt-6.1-terra": (1.0, 0.05, 5.0),
    "gpt-6-cyber": (20.0, 2.0, 100.0),
    "gpt-5.6-sol": (4.0, 0.4, 20.0),
    "gpt-5.6-terra": (2.0, 0.2, 12.0),
    "gpt-5.6-luna": (0.2, 0.02, 1.2),
    "gpt-5.5": (5.0, 0.5, 30.0),
    "gpt-5.5-pro": (30.0, None, 180.0),
    "gpt-5.4": (2.5, 0.25, 15.0),
    "gpt-5.4-mini": (0.75, 0.075, 4.5),
    "gpt-5.4-nano": (0.2, 0.02, 1.25),
    "gpt-5.4-pro": (30.0, None, 180.0),
    "gpt-5.3-codex": (1.75, None, 14.0),
    "gpt-5.3-instant": (1.75, None, 14.0),
    "gpt-5.2-pro": (21.0, None, 168.0),
    "gpt-5.2": (1.75, None, 14.0),
    "gpt-5.2-codex": (1.75, None, 14.0),
    "gpt-5.2-instant": (1.5, None, 6.0),
    "gpt-5.1": (1.25, None, 10.0),
    "gpt-5.1-codex": (1.25, 0.125, 10.0),
    "gpt-5.1-codex-max": (1.25, 0.125, 10.0),
    "gpt-5": (1.25, None, 10.0),
    "gpt-5-mini": (0.25, None, 2.0),
    "gpt-5-nano": (0.05, None, 0.4),
    "gpt-4.5": (75.0, 37.5, 150.0),
    "gpt-4.1": (2.0, None, 8.0),
    "gpt-4.1-mini": (0.4, None, 1.6),
    "gpt-4.1-nano": (0.1, None, 0.4),
    "gpt-4-turbo": (10.0, None, 30.0),
    "gpt-4o": (2.5, None, 10.0),
    "gpt-4o-mini": (0.15, None, 0.6),
    "o4-mini": (1.1, None, 4.4),
    "o3-pro": (20.0, None, 80.0),
    "o3": (2.0, None, 8.0),
    "o3-mini": (1.1, None, 4.4),
    "o1-pro": (150.0, None, 600.0),
    "o1": (15.0, None, 60.0),
    "o1-mini": (1.1, 0.55, 4.4),
}

_DATE_TAIL = re.compile(r"-(?:\d{4}-\d{2}-\d{2}|\d{8})$")
_BY_LENGTH = sorted(PRICES, key=len, reverse=True)


def price_key(model):
    """The PRICES key for a model name such as "gpt-5.4", "openai/gpt-5.4-mini" or "gpt-5.4-2026-03-05", or None."""
    if not isinstance(model, str):
        return None
    name = _DATE_TAIL.sub("", model.strip().lower().rsplit("/", 1)[-1])
    if name in PRICES:
        return name
    for key in _BY_LENGTH:                       # longest first, so "gpt-5.4-mini" beats "gpt-5.4"
        if name.startswith(key + "-"):
            return key
    return None


def row_cost(model, input_tokens, output_tokens, cache_read, cache_write):
    """API list price in US dollars for one usage row, or None when the model has no listed price.

    input_tokens excludes cached and cache-write tokens (they are separate fields)."""
    key = price_key(model)
    if key is None:
        return None
    rate_in, rate_cached, rate_out = PRICES[key]
    write_mult = 1.25 if key.startswith("gpt-6") else 1.0
    cached = rate_in if rate_cached is None else rate_cached
    return (input_tokens * rate_in + cache_read * cached + cache_write * rate_in * write_mult
            + output_tokens * rate_out) / 1e6

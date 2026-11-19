"""Name helpers shared across modules."""


def unbracket(name: str) -> str:
    """'[Sales [Net]]]' or '[Sales [Net]]' -> 'Sales [Net]' (removes exactly one outer bracket pair)."""
    n = (name or "").strip()
    if n.startswith("[") and n.endswith("]"):
        n = n[1:-1].replace("]]", "]")
    return n

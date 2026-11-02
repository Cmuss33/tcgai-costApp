"""Deterministic evidence for the chat evaluator, plus score caps.

The judge (views.score_single_chat) grades the transcript text. Until
2026-11 it never saw the store's own records, so it graded plausibility:
chat c5b596ef took 92 from all five samples while the records show five
self-retractions of invented products, one provably false retraction
(in-stock sleeves retracted as "not in our catalog results" two turns
after they were shown), and an Abuse Guard closure. This module builds
the evidence the app ALREADY stores -- per-message products_shown, the
bot's own retraction text, the conversation's end state -- and applies
caps in code after the judge scores: the judge proposes, code disposes.

What the app does NOT store, and this module therefore cannot use:
search/tool results (the cost log records that a tool call happened,
never its name or output) and any structured guard event (a closure is
visible only as the closure template's text in the final bot message).
Caps key only on what is checkable from the stored rows.
"""
import re
import unicodedata


# --- text plumbing -------------------------------------------------------

def _tokens(text):
    """Lowercased, accent-stripped word tokens."""
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    return re.findall(r"[a-z0-9]+", text)


# Tokens too common across the catalog to identify a product on their own.
_COMMON_TOKENS = {
    "pokemon", "tcg", "the", "a", "an", "of", "and", "or", "for", "with",
    "ex", "gx", "v", "mega", "card", "cards",
}


def _title_tokens(title):
    return set(_tokens(title)) - {"the", "a", "an", "of", "and", "or"}


def _references_title(text_tokens, title):
    """True when the text plausibly names this product: at least two of
    the title's tokens appear, and at least one of them is distinctive
    (not a catalog-common word). "Mega Charizard X ex" shares {mega, ex}
    with half the catalog and must NOT count as naming the Mega Greninja
    collection; "Dragon Shield sleeves" names the Dragon Shield line."""
    matched = _title_tokens(title) & text_tokens
    if len(matched) < 2:
        return False
    return bool(matched - _COMMON_TOKENS)


# --- markers ---------------------------------------------------------------

# The bot's self-correction shapes (its own words in production chats).
RETRACTION_PATTERNS = [
    r"correct my (last|previous) message",
    r"i need to correct",
    r"let me correct",
    r"please disregard",
    r"shouldn'?t have (listed|mentioned|included|said)",
    r"(weren'?t|wasn'?t) (in|from) our (search|catalog) results",
    r"wasn'?t confirmed in our catalog",
    r"i was wrong about",
]

# A retraction becomes FALSE when the same message also denies that the
# retracted thing was ever in the results / in stock.
DENIAL_PATTERNS = [
    r"didn'?t show up",
    r"never showed up",
    r"neither\b[^.]{0,80}(catalog|search) results",
    r"(weren'?t|wasn'?t|isn'?t|not) (in|from) our (search|catalog) results",
    r"not (actually )?(in|from) our (search|catalog) results",
    r"can'?t confirm[^.]{0,60}in stock",
    r"shouldn'?t have (listed|mentioned|included)",
    r"wasn'?t confirmed in our catalog",
]

# The Abuse Guard's closure template (chatbot-side text; the cost app
# stores no structured guard event, so the template's own words in the
# final bot message are the only record a closure happened).
CLOSURE_PATTERNS = [
    r"can'?t continue this conversation",
    r"cannot continue this conversation",
    r"this chat is for product, stock, price, event, and store questions",
]

_RETRACTION_RES = [re.compile(p, re.IGNORECASE) for p in RETRACTION_PATTERNS]
_DENIAL_RES = [re.compile(p, re.IGNORECASE) for p in DENIAL_PATTERNS]
_CLOSURE_RES = [re.compile(p, re.IGNORECASE) for p in CLOSURE_PATTERNS]


def _any_match(patterns, text):
    return any(p.search(text or "") for p in patterns)


def _shown_titles(products_shown):
    """Product titles from one message's products_shown payload
    ({primary: [...], complementary: [...]}, title or name keyed)."""
    titles = []
    if isinstance(products_shown, dict):
        for bucket in ("primary", "complementary"):
            for item in products_shown.get(bucket) or []:
                if isinstance(item, dict):
                    title = item.get("title") or item.get("name")
                    if title:
                        titles.append(title)
    return titles


# --- evidence ---------------------------------------------------------------

def build_evidence(messages):
    """Assemble the deterministic evidence for one conversation from its
    stored Message rows (chronological). Pure: reads attributes only, so
    it is unit-testable without the ORM."""
    messages = list(messages)
    turns = []
    all_titles = []
    retractions = []
    unanswered = 0
    shown_before = []  # titles from strictly earlier turns

    for idx, message in enumerate(messages):
        turn = idx + 1
        titles = _shown_titles(getattr(message, "products_shown", None))
        turns.append({"turn": turn, "products_shown": titles})
        for title in titles:
            if title not in all_titles:
                all_titles.append(title)

        assistant_text = (getattr(message, "returned_content", "") or "").strip()
        if not assistant_text:
            raw = getattr(message, "llm_formatted_returned_message", "") or ""
            if not re.search(r"stop_reason\W{1,6}tool_use", str(raw)):
                unanswered += 1
        elif _any_match(_RETRACTION_RES, assistant_text):
            text_tokens = set(_tokens(assistant_text))
            matched = [t for t in shown_before
                       if _references_title(text_tokens, t)]
            retractions.append({
                "turn": turn,
                "excerpt": " ".join(assistant_text.split())[:200],
                "matched_shown_titles": matched,
                "false": bool(matched) and _any_match(_DENIAL_RES, assistant_text),
            })
        shown_before.extend(t for t in titles if t not in shown_before)

    last_text = ""
    for message in reversed(messages):
        candidate = (getattr(message, "returned_content", "") or "").strip()
        if candidate:
            last_text = candidate
            break
    end_state = "guard_closure" if _any_match(_CLOSURE_RES, last_text) else "normal"

    return {
        "version": 1,
        "turns": turns,
        "products_shown_titles": all_titles[:60],
        "products_shown_total": len(all_titles),
        "retractions": retractions,
        "false_retraction": any(r["false"] for r in retractions),
        "end_state": end_state,
        "unanswered_turns": unanswered,
    }


def render_evidence_block(evidence):
    """The prompt section: only facts that exist, stated as records.
    Returns "" when there is nothing to add (a clean, productless chat
    keeps the transcript-only prompt)."""
    lines = []
    product_turns = [t for t in evidence["turns"] if t["products_shown"]]
    if product_turns:
        per_turn = "; ".join(
            f"Turn {t['turn']}: " + "; ".join(t["products_shown"][:12])
            for t in product_turns
        )
        lines.append(
            f"- Products actually shown to the shopper ({evidence['products_shown_total']} "
            f"distinct): {per_turn}."
        )
    if evidence["retractions"]:
        turns = ", ".join(str(r["turn"]) for r in evidence["retractions"])
        lines.append(
            f"- The assistant corrected/retracted its own earlier statements "
            f"{len(evidence['retractions'])} time(s) (turns {turns})."
        )
        for r in evidence["retractions"]:
            if r["false"]:
                names = "; ".join(r["matched_shown_titles"])
                lines.append(
                    f"- FALSE retraction on turn {r['turn']}: the assistant claimed "
                    f"{names} was not in its results / could not be confirmed in stock, "
                    f"but the records show it WAS shown to the shopper earlier in this "
                    f"same conversation."
                )
    if evidence["end_state"] == "guard_closure":
        lines.append(
            "- How the conversation ended: the store's abuse guard closed the chat. "
            "The final assistant message is the guard's closure notice, not a "
            "shopping answer -- the shopper was cut off."
        )
    if evidence["unanswered_turns"]:
        lines.append(
            f"- Turns where no assistant reply was recorded at all: "
            f"{evidence['unanswered_turns']}."
        )
    if not lines:
        return ""
    header = (
        "EVIDENCE FROM THE STORE'S OWN RECORDS (logged by the system as the "
        "conversation happened -- where these records speak, they override a "
        "charitable reading of the transcript):"
    )
    return header + "\n" + "\n".join(lines)


# --- caps ---------------------------------------------------------------------

CAP_GUARD_CLOSURE = 59
CAP_FALSE_RETRACTION = 39
CAP_RETRACTION_OF_SHOWN = 79


def apply_score_caps(score, evidence):
    """The judge proposes, code disposes. Returns (final_score,
    caps_applied) where caps_applied lists every rule that fired --
    the final score is the minimum of the judge's score and all caps.

    - guard_closure (59): the conversation ended with the shopper cut
      off by the abuse guard; no framing of the last answer undoes that.
    - false_retraction (39): the bot retracted a product the records
      show was presented, claiming it wasn't in the results -- the
      rubric's own 1-39 band covers invented facts, and a false
      retraction is one with paperwork.
    - retraction_of_shown_product (79): a self-correction about a
      product that WAS in the presented results keeps the chat out of
      the 90s. Retractions whose subjects never appear in the shown
      evidence (the bot correcting its own inventions, or non-product
      statements) do NOT fire this cap -- per the grounding design, the
      cap keys on retractions of what was actually in the results.
    """
    caps = []
    if evidence.get("end_state") == "guard_closure":
        caps.append({"rule": "guard_closure", "cap": CAP_GUARD_CLOSURE})
    if evidence.get("false_retraction"):
        caps.append({"rule": "false_retraction", "cap": CAP_FALSE_RETRACTION})
    elif any(r["matched_shown_titles"] for r in evidence.get("retractions", [])):
        caps.append({"rule": "retraction_of_shown_product",
                     "cap": CAP_RETRACTION_OF_SHOWN})
    final = min([score] + [c["cap"] for c in caps])
    return final, caps

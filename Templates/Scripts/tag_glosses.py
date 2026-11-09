# tag_glosses.py — synonym-rich glosses for retrieval-augmented tagging.
#
# tag_clippings_rag.py embeds (tag + gloss) so it can rank your taxonomy by
# relevance to a note and hand the model a short, focused candidate list instead
# of the entire allowlist. A good gloss contains the words a *note about this
# topic* would actually use -- NOT a dictionary definition of the tag name. That
# closes the vocabulary gap between a terse tag ("FinancialReporting") and how the
# topic reads in prose ("chargeback, cost recovery, internal pricing").
#
# THIS IS AN EXAMPLE. Replace CURATED below with one entry per tag in your own
# Knowledge/Tag Taxonomy.md. Tags you don't gloss fall back to an auto-derived
# gloss (the de-CamelCased name), which works but ranks less well.

CURATED = {
    # --- a few illustrative entries; swap in your own taxonomy ---
    "AI": "artificial intelligence machine learning models neural networks",
    "AI/Agents": "agentic ai autonomous agents tool-use agent workflows",
    "AI/LLMs": "large language models LLM foundation models open-weight",
    "Cybersecurity": "information security infosec breach threat phishing ransomware authentication encryption zero-trust",
    "Infrastructure": "data center power grid network storage capacity buildout cabling cooling",
    "ProjectManagement": "planning milestones timeline delivery roadmap scope stakeholders",
    "Programming": "code software development python script engineering API debugging",
    "Legislation": "regulation policy law act bill federal rule statute compliance",
    "FutureOfWork": "workforce impact jobs employment displacement reskilling labor market",
    "Recipes/Seafood": "seafood fish shellfish shrimp crab salmon",
    "Travel": "trip flight hotel destination vacation itinerary tourism",
    "Photography": "camera lens photo image exposure composition aperture",
    "Health/Exercise": "workout training fitness cardio strength gym routine",
    "Vendors/Microsoft": "Microsoft Azure M365 Copilot Windows Entra Teams",
    "Vendors/AWS": "Amazon Web Services AWS cloud EC2 S3 hyperscaler",
}


# A named-entity tag survives only if the entity actually appears in the note.
# Stops an AI/cloud-heavy corpus from over-applying vendor/product tags to notes
# that merely discuss the general field, and stops the model from inferring a
# vendor from world knowledge (a note about executive search firms tagged with
# a consultancy it never names). Any Vendors/* tag not listed here falls back to
# its own leaf name. Both taggers use this. Extend for your taxonomy here, or
# per-install without editing code via TAGGER_ENTITY_ALIASES in
# ~/dev/secrets/.env:  "Vendors/Workday=workday,myprogram;Vendors/Acme=acme"
NAMED_ENTITY_ALIASES = {
    "Vendors/AWS": ["aws", "amazon web services", "amazon", "bedrock"],
    "Vendors/Azure": ["azure"],
    "Vendors/GCP": ["gcp", "google cloud", "vertex"],
    "Vendors/Google": ["google", "gemini", "gmail"],
    "Vendors/Microsoft": ["microsoft", "m365", "o365", "office 365", "copilot",
                          "windows", "entra", "azure", "teams", "sharepoint",
                          "onedrive", "outlook", "intune", "defender"],
    "Vendors/NVIDIA": ["nvidia", "cuda"],
    "Vendors/OpenAI": ["openai", "chatgpt", "gpt-"],
    "Vendors/Anthropic": ["anthropic", "claude"],
    "Vendors/ServiceNow": ["servicenow", "service now"],
    "Vendors/Salesforce": ["salesforce"],
    "AI/Claude": ["claude", "anthropic"],
    "AI/Copilot": ["copilot"],
}


def _entity_aliases() -> dict:
    import os
    aliases = {k: list(v) for k, v in NAMED_ENTITY_ALIASES.items()}
    for entry in os.environ.get("TAGGER_ENTITY_ALIASES", "").split(";"):
        tag, sep, words = entry.partition("=")
        if sep and tag.strip():
            extra = [w.strip().lower() for w in words.split(",") if w.strip()]
            aliases.setdefault(tag.strip(), []).extend(extra)
    return aliases


def entity_supported(tag: str, text_lower: str) -> bool:
    """True unless `tag` names an entity that `text_lower` never mentions.
    Aliases match at a word start, so "gpt-" still catches "gpt-4o" but
    "dell" does not match "wendell"."""
    import re
    aliases = _entity_aliases().get(tag)
    if aliases is None and tag.startswith("Vendors/"):
        aliases = [tag.split("/", 1)[1].lower()]
    if not aliases:
        return True
    return any(re.search(r"\b" + re.escape(a), text_lower) for a in aliases)


def derive_gloss(tag: str) -> str:
    """Auto-gloss for tags not in CURATED: de-CamelCase the leaf and prepend the
    de-CamelCased parent context."""
    import re

    def decamel(s: str) -> str:
        s = s.replace("-", " ").replace("_", " ")
        s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
        return s

    if "/" in tag:
        parent, leaf = tag.split("/", 1)
        return f"{decamel(parent)} {decamel(leaf)}".lower()
    return decamel(tag).lower()


def gloss_for(tag: str) -> str:
    base = derive_gloss(tag)               # always include the literal tag words
    extra = CURATED.get(tag, "")
    return f"{base} {extra}".strip()

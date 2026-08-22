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

"""The report's section taxonomy and per-section freshness classification.

The briefing has nine rendered sections (PRD §5): seven citation-backed content
sections plus **Unknowns** and **Sources**. Facts belong to the seven content
sections; Unknowns/Sources are derived at assembly time.
"""

from __future__ import annotations

from enum import Enum


class Section(str, Enum):
    """The seven citation-backed content sections (in render order)."""

    COMPANY_OVERVIEW = "Company Overview"
    PRODUCTS_SERVICES = "Products & Services"
    TARGET_CUSTOMERS = "Target Customers"
    BUSINESS_SIGNALS = "Business Signals"
    RISKS_CHALLENGES = "Risks & Challenges"
    DISCOVERY_QUESTIONS = "Suggested Discovery Questions"
    OUTREACH_STRATEGY = "Suggested Outreach Strategy"


CONTENT_SECTIONS: tuple[Section, ...] = tuple(Section)


class FreshnessClass(str, Enum):
    """Stored per fact (§7.1) so recall re-fetches news but warm-starts the rest."""

    DURABLE = "durable"
    VOLATILE = "volatile"


# News-like sections decay fast; everything else warm-starts from memory.
_VOLATILE_SECTIONS: frozenset[Section] = frozenset(
    {Section.BUSINESS_SIGNALS, Section.RISKS_CHALLENGES}
)


def freshness_for(section: Section) -> FreshnessClass:
    return (
        FreshnessClass.VOLATILE
        if section in _VOLATILE_SECTIONS
        else FreshnessClass.DURABLE
    )

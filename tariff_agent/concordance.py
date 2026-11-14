"""Topic concordance: links notice wording to product families and HTS prefixes.

Investigation notices rarely list HTS codes ("imports of wind turbines and their parts").
This table lets the matcher propose candidate products for such notices. Matches made
through it are labelled "category concordance" and carry a lower strength than a match
on HTS codes printed in the notice itself; with an API key the LLM reviews them.

The HTS prefixes are a prototype-level mapping written by hand, not an official scope.
"""
from __future__ import annotations

import re

# slug -> (regex over notice title/categories, HTS prefixes, product-description regex)
TOPICS: dict[str, dict] = {
    "steel": {"notice": r"\bsteel\b", "hts": ["72", "73"], "product": r"\bsteel\b"},
    "aluminum": {"notice": r"\balumin(?:i)?um\b", "hts": ["76"], "product": r"\balumin(?:i)?um\b"},
    "copper": {"notice": r"\bcopper\b", "hts": ["74"], "product": r"\bcopper\b|\bbrass\b"},
    "polysilicon": {"notice": r"polysilicon", "hts": ["280461", "3818", "854142", "854143"],
                    "product": r"polysilicon|solar (?:cell|module|panel)|silicon wafer"},
    "unmanned_aircraft": {"notice": r"unmanned aircraft|\bUAS\b|\bdrones?\b", "hts": ["8806"],
                          "product": r"\bdrones?\b|unmanned aircraft"},
    "wind_turbines": {"notice": r"wind turbines?", "hts": ["850231", "841290", "730820"],
                      "product": r"wind[- ](?:turbine|powered)"},
    "pharmaceuticals": {"notice": r"pharmaceutical", "hts": ["30", "2936", "2937", "2941"],
                        "product": r"pharmaceutical|\bAPI\b|medicament"},
    "semiconductors": {"notice": r"semiconductor", "hts": ["8541", "8542", "8486"],
                       "product": r"semiconductor|integrated circuit|\bchips?\b"},
    "critical_minerals": {"notice": r"critical minerals?", "hts": ["2504", "2605", "2805", "2825", "8105", "8112"],
                          "product": r"rare earth|cobalt|lithium (?:carbonate|hydroxide)|graphite"},
    "timber_lumber": {"notice": r"\btimber\b|\blumber\b", "hts": ["4403", "4407", "940161", "940340"],
                      "product": r"\blumber\b|\btimber\b|kitchen cabinet|upholstered"},
    "trucks": {"notice": r"medium- and heavy-duty|\btrucks\b", "hts": ["8701", "8704"],
               "product": r"\btrucks?\b"},
    "automobiles": {"notice": r"automobiles?", "hts": ["8703", "8708"], "product": r"\bauto(?:mobile)? parts?\b|passenger vehicle"},
    "robotics_machinery": {"notice": r"robotics|industrial machinery", "hts": ["847950", "8456", "8457", "8458", "8459", "8460", "8461", "8462", "8463"],
                           "product": r"\brobot|machine tool|\bCNC\b"},
    "aircraft": {"notice": r"commercial aircraft|jet engines?", "hts": ["8802", "841111", "841112"],
                 "product": r"jet engine|aircraft engine"},
    "ppe_medical": {"notice": r"personal protective equipment|medical consumables", "hts": ["4015", "630790", "9018"],
                    "product": r"surgical|respirator|medical gloves|syringe"},
    "maritime_shipbuilding": {"notice": r"maritime|shipbuilding", "hts": ["8901", "842619"],
                              "product": r"ship-to-shore|\bvessels?\b"},
    "anthracite_coal": {"notice": r"anthracite", "hts": ["2701"], "product": r"\bcoal\b"},
}

# Broad, non-product-specific programmes: tracked as topics so later notices supersede
# earlier ones, but matched to products by country rather than by HTS code.
BROAD_TOPICS: dict[str, str] = {
    "import_surcharge_122": r"temporary import surcharge",
    "reciprocal_tariffs": r"reciprocal tariff",
    "china_301_tech_transfer": r"technology transfer, intellectual property",
    "301_forced_labor": r"forced labor",
    "301_excess_capacity": r"structural excess capacity",
    "ieepa_tariff_actions": r"ending certain tariff actions",
    "canada_offset_duties": r"canadian discrimination",
    "brazil": r"\bbrazil",
    "nicaragua": r"\bnicaragua",
    "vietnam_ip": r"vietnam's acts",
    "germany_pharma_pricing": r"germany's persistent",
    "china_phase_one": r"phase one agreement",
    "china_semiconductors_301": r"targeting of the semiconductor industry",
    "fentanyl_border_duties": r"synthetic opioid|illicit drugs|southern border|northern border",
}

_COMPILED = {k: re.compile(v["notice"], re.I) for k, v in TOPICS.items()}
_COMPILED_BROAD = {k: re.compile(v, re.I) for k, v in BROAD_TOPICS.items()}
_PRODUCT = {k: re.compile(v["product"], re.I) for k, v in TOPICS.items()}


def topics_for_notice(title: str, categories: list[str] | None = None) -> list[str]:
    """Topic slugs for a notice, derived from its title (and extracted categories)."""
    title = title or ""
    broad = [k for k, rx in _COMPILED_BROAD.items() if rx.search(title)]
    # A programme-level 301 notice is its own topic even if its title names a sector.
    if "china_semiconductors_301" in broad:
        return ["china_semiconductors_301"]
    product = [k for k, rx in _COMPILED.items() if rx.search(title)]
    if not product and not broad:
        hay = " ; ".join(categories or [])
        product = [k for k, rx in _COMPILED.items() if rx.search(hay)]
    return product or broad


def hts_prefixes(topic: str) -> list[str]:
    return TOPICS.get(topic, {}).get("hts", [])


def product_mentions_topic(topic: str, description: str) -> bool:
    rx = _PRODUCT.get(topic)
    return bool(rx and rx.search(description or ""))

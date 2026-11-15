"""Authoritative City -> Area / Neighbourhood Directory for RingScout Consumer UI & Maps Grounding.

Provides:
1. Dropdown options for 10 major Indian metro areas (~130 neighbourhoods) so users select areas cleanly
   instead of guessing spellings.
2. Exact postal PIN codes & sub-locality micro-tags (e.g. Bandra West 400050 vs Bandra East / BKC 400051)
   so Google Maps discovery prompts and Tier-0 proximity filters never mix adjacent East/West pockets.
"""

from __future__ import annotations

from typing import Any

INDIA_CITY_AREAS: dict[str, list[dict[str, Any]]] = {
    "Mumbai": [
        {"name": "Bandra West", "label": "Bandra West (Linking Rd · Hill Rd · Pali Hill)", "pins": [400050], "aliases": ["pali hill", "linking road", "hill road", "carter road", "bandstand", "waterfield", "turner road", "st andrews"]},
        {"name": "Bandra East", "label": "Bandra East / BKC (Kala Nagar · Kherwadi)", "pins": [400051], "aliases": ["bandra kurla complex", "bkc", "kala nagar", "kherwadi", "mig colony", "government colony"]},
        {"name": "Khar West", "label": "Khar West (14th Rd · SV Rd · Khar Danda)", "pins": [400052], "aliases": ["khar danda", "14th road", "16th road khar", "pali mala"]},
        {"name": "Santacruz West", "label": "Santacruz West (Juhu Tara · Milan Subway)", "pins": [400054], "aliases": ["juhu tara", "linking road santacruz", "podar"]},
        {"name": "Santacruz East", "label": "Santacruz East (Kalina · Vakola)", "pins": [400055, 400098], "aliases": ["kalina", "vakola", "cst road"]},
        {"name": "Juhu", "label": "Juhu (JVPD · Juhu Beach · Gulmohar Rd)", "pins": [400049], "aliases": ["jvpd", "juhu beach", "gulmohar road", "ab nair road"]},
        {"name": "Andheri West", "label": "Andheri West (Lokhandwala · Versova · Oshiwara)", "pins": [400053, 400061], "aliases": ["lokhandwala", "versova", "oshiwara", "infinity mall", "four bungalows", "seven bungalows", "veera desai", "link road andheri"]},
        {"name": "Andheri East", "label": "Andheri East (Chakala · Marol · MIDC · Saki Naka)", "pins": [400059, 400069, 400093], "aliases": ["chakala", "marol", "saki naka", "jb nagar", "mahakali", "midc andheri", "seepz"]},
        {"name": "Lower Parel", "label": "Lower Parel (Kamala Mills · Phoenix Palladium)", "pins": [400013], "aliases": ["kamala mills", "senapati bapat", "delisle road", "high street phoenix"]},
        {"name": "Worli", "label": "Worli (Worli Sea Face · Annie Besant Rd)", "pins": [400018, 400030], "aliases": ["worli sea face", "prabhadevi", "century bazaar", "atria"]},
        {"name": "Dadar West", "label": "Dadar West (Shivaji Park · Gokhale Rd)", "pins": [400028], "aliases": ["shivaji park", "gokhale road", "ranade road", "kelkar road", "mahim"]},
        {"name": "Colaba", "label": "Colaba & Fort (Causeway · Kala Ghoda · Nariman Pt)", "pins": [400001, 400005, 400021], "aliases": ["colaba causeway", "kala ghoda", "fort", "churchgate", "nariman point", "marine drive", "cuffe parade"]},
        {"name": "Malabar Hill", "label": "South Mumbai (Breach Candy · Kemps Corner · Pedder Rd)", "pins": [400006, 400026, 400007], "aliases": ["breach candy", "kemps corner", "pedder road", "nepean sea", "tardeo", "grant road"]},
        {"name": "Powai", "label": "Powai (Hiranandani Gardens · Chandivali)", "pins": [400076, 400072], "aliases": ["hiranandani", "chandivali", "iit powai", "galleria"]},
        {"name": "Chembur", "label": "Chembur (Diamond Garden · Sindhi Society)", "pins": [400071, 400089], "aliases": ["diamond garden", "sindhi society", "tilak nagar"]},
        {"name": "Ghatkopar East", "label": "Ghatkopar (Pant Nagar · R City · Vikroli)", "pins": [400075, 400077, 400086], "aliases": ["pant nagar", "ghatkopar west", "r city", "vidyavihar"]},
        {"name": "Goregaon East", "label": "Goregaon (Oberoi Mall · Film City Rd · Mindspace)", "pins": [400063, 400062, 400104], "aliases": ["goregaon west", "arey", "oberoi garden", "jawahar nagar"]},
        {"name": "Malad West", "label": "Malad West (Mindspace · Orlem · Inorbit)", "pins": [400064], "aliases": ["mindspace", "orlem", "inorbit", "chincholi", "marve road"]},
        {"name": "Borivali West", "label": "Borivali & Kandivali West (IC Colony · Mahavir Nagar)", "pins": [400092, 400103, 400067], "aliases": ["ic colony", "shimpoli", "mahavir nagar", "kandivali west", "thakur village"]},
        {"name": "Thane West", "label": "Thane West (Viviana · Ghodbunder Rd · Hiranandani Estate)", "pins": [400601, 400607, 400610], "aliases": ["ghodbunder", "majiwada", "panchpakhadi", "naupada", "hiranandani estate"]},
        {"name": "Vashi", "label": "Navi Mumbai (Vashi · Seawoods · Belapur · Nerul)", "pins": [400703, 400706, 400614], "aliases": ["seawoods", "nerul", "cbd belapur", "kharghar", "palm beach road"]},
    ],
    "Bengaluru": [
        {"name": "Indiranagar", "label": "Indiranagar (100 Feet Rd · 12th Main · CMH Rd)", "pins": [560038, 560008], "aliases": ["100 feet road", "100ft road", "12th main", "cmh road", "hal 2nd stage", "domlur"]},
        {"name": "Koramangala", "label": "Koramangala (4th, 5th, 6th & 7th Block · Sony Signal)", "pins": [560034, 560095], "aliases": ["5th block", "4th block", "6th block", "7th block", "80 feet road koramangala", "jyoti nivas"]},
        {"name": "HSR Layout", "label": "HSR Layout (27th Main · Sector 1–7)", "pins": [560102], "aliases": ["27th main", "sector 1 hsr", "sector 2 hsr", "sector 7 hsr", "agara"]},
        {"name": "Whitefield", "label": "Whitefield (ITPL · Brookefield · Phoenix Marketcity)", "pins": [560066, 560037, 560048], "aliases": ["itpl", "brookefield", "mahadevapura", "varthur", "kadugodi"]},
        {"name": "MG Road", "label": "Central Bengaluru (MG Rd · Brigade Rd · Lavelle Rd · UB City)", "pins": [560001, 560025], "aliases": ["brigade road", "church street", "lavelle road", "ub city", "ashok nagar", "richmond road", "residency road"]},
        {"name": "Jayanagar", "label": "Jayanagar (4th Block · 9th Block · South End)", "pins": [560011, 560041, 560069], "aliases": ["4th block jayanagar", "9th block jayanagar", "basavanagudi"]},
        {"name": "JP Nagar", "label": "JP Nagar (Phases 1–7 · Bannerghatta Rd)", "pins": [560078, 560076], "aliases": ["bannerghatta", "jp nagar 2nd phase", "jp nagar 6th phase", "bilekahalli"]},
        {"name": "Bellandur", "label": "Bellandur & Sarjapur Road (ORR · EcoSpace)", "pins": [560103, 560035], "aliases": ["sarjapur road", "ecospace", "haralur", "kasavanahalli", "iblur"]},
        {"name": "Marathahalli", "label": "Marathahalli & AECS Layout", "pins": [560037], "aliases": ["aecs layout", "kundalahalli", "munnekollal"]},
        {"name": "Malleshwaram", "label": "Malleshwaram & Rajajinagar (8th Cross · Orion)", "pins": [560003, 560055, 560010], "aliases": ["rajajinagar", "sadashivanagar", "sampige road", "margosa road"]},
        {"name": "Kalyan Nagar", "label": "Kammanahalli, Kalyan Nagar & HRBR Layout", "pins": [560043, 560084], "aliases": ["kammanahalli", "hrbr layout", "banaswadi"]},
        {"name": "Hebbal", "label": "Hebbal, Sahakara Nagar & Manyata Tech Park", "pins": [560024, 560092, 560045], "aliases": ["sahakara nagar", "manyata", "yelahanka", "rt nagar"]},
        {"name": "Electronic City", "label": "Electronic City (Phase 1 & Phase 2)", "pins": [560100], "aliases": ["neeladri", "electronic city phase 1"]},
    ],
    "Delhi NCR": [
        {"name": "Connaught Place", "label": "Connaught Place & Central Delhi (CP · Khan Market)", "pins": [110001, 110003], "aliases": ["cp", "janpath", "barakhamba", "khan market", "mandi house"]},
        {"name": "Hauz Khas", "label": "Hauz Khas, Green Park & SDA", "pins": [110016], "aliases": ["green park", "sda", "auurobindo marg", "hauz khas village"]},
        {"name": "Greater Kailash", "label": "Greater Kailash (GK-1 & GK-2 · M Block · Nehru Place)", "pins": [110048, 110019], "aliases": ["gk 1", "gk 2", "m block", "nehru place", "chittaranjan park", "kailash colony"]},
        {"name": "Saket", "label": "Saket & Malviya Nagar (Select Citywalk)", "pins": [110017], "aliases": ["malviya nagar", "select citywalk", "panchsheel"]},
        {"name": "South Extension", "label": "South Extension, Defence Colony & Lajpat Nagar", "pins": [110049, 110024], "aliases": ["defence colony", "lajpat nagar", "south ex"]},
        {"name": "Vasant Kunj", "label": "Vasant Kunj, Vasant Vihar & Aerocity", "pins": [110070, 110057, 110037], "aliases": ["vasant vihar", "aerocity", "mahipalpur", "ambience mall"]},
        {"name": "Punjabi Bagh", "label": "West Delhi (Rajouri Garden · Punjabi Bagh · Janakpuri)", "pins": [110026, 110027, 110058], "aliases": ["rajouri garden", "janakpuri", "paschim vihar", "patel nagar"]},
        {"name": "Rohini", "label": "North Delhi (Pitampura · Model Town · Rohini)", "pins": [110085, 110034, 110009], "aliases": ["pitampura", "model town", "shalimar bagh", "kamla nagar"]},
        {"name": "Dwarka", "label": "Dwarka (Sectors 6, 7, 10, 12)", "pins": [110075, 110077], "aliases": ["dwarka sector"]},
        {"name": "DLF Cyber City", "label": "Gurugram — DLF CyberHub, Phase 1–5 & MG Road", "pins": [122002, 122009], "aliases": ["cyber hub", "cyberhub", "dlf phase", "mg road gurgaon", "udyog vihar", "galleria market"]},
        {"name": "Golf Course Road", "label": "Gurugram — Golf Course Rd, Sector 43, 53, 54 & Sohna Rd", "pins": [122011, 122018], "aliases": ["sector 54", "sector 53", "sector 43", "sohna road", "golf course extension"]},
        {"name": "Noida Sector 18", "label": "Noida — Sector 18, Sector 62 & Expressway", "pins": [201301, 201309, 201304], "aliases": ["sector 18 noida", "sector 62", "sector 104", "dlf mall of india"]},
    ],
    "Hyderabad": [
        {"name": "Jubilee Hills", "label": "Jubilee Hills (Road No. 36 & 45 · Film Nagar)", "pins": [500033, 500096], "aliases": ["road no 36", "road no 45", "film nagar"]},
        {"name": "Banjara Hills", "label": "Banjara Hills (Road No. 1, 10 & 12)", "pins": [500034], "aliases": ["road no 12", "road no 1", "panjagutta", "somajiguda"]},
        {"name": "HITEC City", "label": "HITEC City & Madhapur (Mindspace · Inorbit)", "pins": [500081], "aliases": ["madhapur", "hitech city", "kavuri hills", "ayyappa society"]},
        {"name": "Gachibowli", "label": "Gachibowli, Kondapur & Financial District", "pins": [500032, 500084], "aliases": ["kondapur", "financial district", "nanakramguda", "kokapet"]},
        {"name": "Kukatpally", "label": "Kukatpally (KPHB) & Miyapur", "pins": [500072, 500085], "aliases": ["kphb", "miyapur", "nizampet"]},
        {"name": "Secunderabad", "label": "Secunderabad & Begumpet (Sainikpuri · Karkhana)", "pins": [500003, 500016, 500009], "aliases": ["begumpet", "karkhana", "sainikpuri", "trimulgherry"]},
    ],
    "Pune": [
        {"name": "Koregaon Park", "label": "Koregaon Park & Kalyani Nagar", "pins": [411001, 411006], "aliases": ["kalyani nagar", "kp", "north main road", "mundhwa"]},
        {"name": "Viman Nagar", "label": "Viman Nagar & Kharadi (Phoenix Marketcity · EON)", "pins": [411014], "aliases": ["kharadi", "wadgaon sheri", "nagar road"]},
        {"name": "Baner", "label": "Baner, Balewadi High Street & Aundh", "pins": [411045, 411007], "aliases": ["balewadi", "aundh", "pashan"]},
        {"name": "Kothrud", "label": "Kothrud, Deccan Gymkhana & FC Road", "pins": [411038, 411004], "aliases": ["deccan", "fc road", "jm road", "shivajinagar", "prabhat road", "karve nagar"]},
        {"name": "Wakad", "label": "Wakad, Hinjewadi & Pimple Saudagar", "pins": [411057, 411027], "aliases": ["hinjewadi", "hinjawadi", "pimple saudagar"]},
        {"name": "Hadapsar", "label": "Magarpatta, Hadapsar & Wanowrie", "pins": [411028, 411040], "aliases": ["magarpatta", "amanora", "wanowrie", "nibm"]},
    ],
    "Chennai": [
        {"name": "Adyar", "label": "Adyar, Besant Nagar & Thiruvanmiyur", "pins": [600020, 600090, 600041], "aliases": ["besant nagar", "thiruvanmiyur", "lb road"]},
        {"name": "Alwarpet", "label": "Alwarpet, Mylapore & Nungambakkam", "pins": [600018, 600004, 600034], "aliases": ["nungambakkam", "mylapore", "knk road", "khader nawaz", "ttd road"]},
        {"name": "T Nagar", "label": "T. Nagar & Kodambakkam", "pins": [600017, 600024], "aliases": ["thyagaraya nagar", "pondicherry bazaar", "pondy bazaar", "usman road"]},
        {"name": "Anna Nagar", "label": "Anna Nagar (East & West · 2nd Ave)", "pins": [600040, 600102], "aliases": ["anna nagar east", "anna nagar west", "kilpauk"]},
        {"name": "Velachery", "label": "Velachery, Guindy & OMR (Thoraipakkam)", "pins": [600042, 600032, 600097], "aliases": ["thoraipakkam", "perungudi", "omr", "guindy", "phoenix marketcity chennai"]},
    ],
    "Kolkata": [
        {"name": "Park Street", "label": "Park Street, Camac Street & Elgin", "pins": [700016, 700017, 700020], "aliases": ["camac street", "elgin", "shakespeare sarani", "bhowanipore"]},
        {"name": "Ballygunge", "label": "Ballygunge, Gariahat & Southern Avenue", "pins": [700019, 700029], "aliases": ["gariahat", "southern avenue", "hindustan park", "lake market"]},
        {"name": "Salt Lake", "label": "Salt Lake (Sector I, II, III & V)", "pins": [700064, 700091], "aliases": ["bidhannagar", "sector v", "sector 5", "city centre 1"]},
        {"name": "New Town", "label": "New Town & Rajarhat (Action Area I–III)", "pins": [700156, 700135], "aliases": ["rajarhat", "action area", "ecopark"]},
        {"name": "Alipore", "label": "Alipore & New Alipore", "pins": [700027, 700053], "aliases": ["new alipore"]},
    ],
    "Ahmedabad": [
        {"name": "SG Highway", "label": "SG Highway, Bodakdev & Sindhu Bhavan Rd", "pins": [380054, 380059], "aliases": ["bodakdev", "sindhu bhavan", "sbr", "thaltej"]},
        {"name": "Prahlad Nagar", "label": "Prahlad Nagar & Satellite", "pins": [380015], "aliases": ["satellite", "anand nagar", "jodhpur"]},
        {"name": "CG Road", "label": "CG Road, Navrangpura & Vastrapur", "pins": [380009, 380006], "aliases": ["navrangpura", "vastrapur", "law garden", "ellisbridge"]},
    ],
    "Jaipur": [
        {"name": "C Scheme", "label": "C-Scheme, MI Road & Civil Lines", "pins": [302001, 302006], "aliases": ["mi road", "ashok nagar jaipur", "civil lines"]},
        {"name": "Malviya Nagar Jaipur", "label": "Malviya Nagar & WTP / Tonk Road", "pins": [302017, 302018], "aliases": ["world trade park", "tonk road", "durgapura"]},
        {"name": "Vaishali Nagar", "label": "Vaishali Nagar & Mansarovar", "pins": [302021, 302020], "aliases": ["mansarovar", "queens road"]},
    ],
    "Goa": [
        {"name": "Panjim", "label": "Panjim, Miramar & Dona Paula", "pins": [403001, 403004], "aliases": ["panaji", "miramar", "dona paula", "fontainhas"]},
        {"name": "Candolim", "label": "North Goa (Candolim · Calangute · Baga · Assagao · Anjuna)", "pins": [403515, 403516, 403509, 403507], "aliases": ["calangute", "baga", "anjuna", "vagator", "assagao", "siolim", "porvorim"]},
        {"name": "Margao", "label": "South Goa (Margao · Colva · Benaulim · Cavelossim)", "pins": [403601, 403708, 403716], "aliases": ["colva", "benaulim", "cavelossim", "varca"]},
    ],
}


def lookup_area_metadata(city: str, locality: str) -> dict[str, Any] | None:
    """Return `{name, label, pins, aliases}` for `(city, locality)` if present in `INDIA_CITY_AREAS`."""
    loc_low = (locality or "").strip().lower()
    if not loc_low:
        return None
    city_clean = (city or "").strip()
    search_lists: list[list[dict[str, Any]]] = []
    for k, areas in INDIA_CITY_AREAS.items():
        if k.lower() == city_clean.lower():
            search_lists.insert(0, areas)
        else:
            search_lists.append(areas)

    for areas in search_lists:
        for entry in areas:
            if entry["name"].lower() == loc_low:
                return entry
    for areas in search_lists:
        for entry in areas:
            if any(a == loc_low for a in entry.get("aliases", [])):
                return entry
    return None

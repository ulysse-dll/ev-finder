"""
Moteur de calcul EV+ (Expected Value positive).
Compare les cotes Winamax aux probabilites reelles derivees
du consensus pondere multi-bookmakers.

Supporte les marches : h2h (1X2), over_under, btts.
"""

from difflib import SequenceMatcher


def implied_probability(decimal_odds):
    """Convertit une cote decimale en probabilite implicite."""
    if decimal_odds <= 0:
        return 0
    return 1.0 / decimal_odds


def devig_odds(outcomes):
    """
    Supprime la marge du bookmaker (vig) par methode additive.
    Normalise les probabilites implicites pour qu'elles somment a 100%.
    """
    implied_probs = [implied_probability(o["odds"]) for o in outcomes]
    total = sum(implied_probs)

    if total == 0:
        return outcomes

    result = []
    for o, ip in zip(outcomes, implied_probs):
        fair_prob = ip / total
        result.append({
            **o,
            "implied_prob": round(ip, 4),
            "fair_prob": round(fair_prob, 4),
        })
    return result


def calculate_ev(winamax_odds, true_probability):
    """
    EV = (probabilite_reelle x cote) - 1
    Retourne EV en pourcentage.
    """
    ev = (true_probability * winamax_odds) - 1
    return round(ev * 100, 2)


def _normalize_name(name):
    """Normalise un nom d'equipe pour le matching."""
    n = name.lower().strip()
    for remove in ["fc ", " fc", "ac ", " ac", "sc ", " sc", "as ", " as",
                    "ss ", " ss", "us ", " us", "rc ", " rc"]:
        n = n.replace(remove, " ")
    return " ".join(n.split())


def _similarity(a, b):
    """Score de similarite entre deux chaines."""
    return SequenceMatcher(None, _normalize_name(a), _normalize_name(b)).ratio()


def match_events(winamax_events, reference_events, threshold=0.55):
    """
    Match les evenements Winamax avec les evenements de reference
    par fuzzy matching sur les noms d'equipes.
    """
    matched = []
    used_refs = set()

    for wm in winamax_events:
        best_match = None
        best_score = 0

        for i, ref in enumerate(reference_events):
            if i in used_refs:
                continue

            home_score = _similarity(wm.get("home", ""), ref.get("home_team", ""))
            away_score = _similarity(wm.get("away", ""), ref.get("away_team", ""))
            score = (home_score + away_score) / 2

            home_inv = _similarity(wm.get("home", ""), ref.get("away_team", ""))
            away_inv = _similarity(wm.get("away", ""), ref.get("home_team", ""))
            score_inv = (home_inv + away_inv) / 2

            best_local = max(score, score_inv)

            if best_local > best_score and best_local >= threshold:
                best_score = best_local
                best_match = (i, ref)

        if best_match:
            used_refs.add(best_match[0])
            matched.append((wm, best_match[1]))

    return matched


# ── Normalisation des outcomes par marche ──

def _normalize_ou_side(name):
    """Retourne 'over' ou 'under' depuis le nom d'un outcome O/U."""
    n = name.lower()
    if any(k in n for k in ["plus", "over"]):
        return "over"
    if any(k in n for k in ["moins", "under"]):
        return "under"
    return None


def _normalize_btts_side(name):
    """Retourne 'yes' ou 'no' depuis le nom d'un outcome BTTS."""
    n = name.lower()
    if any(k in n for k in ["oui", "yes"]):
        return "yes"
    if any(k in n for k in ["non", "no"]):
        return "no"
    return None


def _get_market_type(event):
    """Determine le market_type d'un event (Winamax ou reference)."""
    t = event.get("market_type")
    if t:
        return t
    m = event.get("market", "h2h")
    if isinstance(m, str):
        if m.startswith("over_under"):
            return "over_under"
        if m == "btts":
            return "btts"
    return "h2h"


# ── Sub-finders par marche ──

def _find_vb_h2h(wm_events, ref_events, min_ev):
    """Trouve les value bets sur le marche H2H (1X2)."""
    value_bets = []
    matches = match_events(wm_events, ref_events)

    for wm_event, ref_event in matches:
        ref_outcomes = devig_odds(ref_event.get("outcomes", []))
        fair_probs = {_normalize_name(o["name"]): o["fair_prob"] for o in ref_outcomes}

        for wm_outcome in wm_event.get("outcomes", []):
            wm_name = _normalize_name(wm_outcome["name"])
            wm_odds = wm_outcome["odds"]

            best_prob = None
            best_sim = 0
            for ref_name, prob in fair_probs.items():
                sim = _similarity(wm_name, ref_name)
                if sim > best_sim and sim > 0.5:
                    best_sim = sim
                    best_prob = prob

            # Fallback "Match nul" / "Draw" / "X"
            if best_prob is None:
                draw_names = {"match nul", "nul", "draw", "x", "tie"}
                if wm_name in draw_names:
                    for ref_name, prob in fair_probs.items():
                        if ref_name in draw_names:
                            best_prob = prob
                            break

            if best_prob is None:
                continue

            ev = calculate_ev(wm_odds, best_prob)
            if ev > min_ev and ev < 50:
                value_bets.append({
                    "sport": wm_event.get("sport", ref_event.get("sport_title", "?")),
                    "home": wm_event.get("home", ref_event.get("home_team", "")),
                    "away": wm_event.get("away", ref_event.get("away_team", "")),
                    "market": wm_event.get("market", "h2h"),
                    "market_type": "h2h",
                    "market_threshold": None,
                    "bet_on": wm_outcome["name"],
                    "winamax_odds": wm_odds,
                    "fair_prob": round(best_prob * 100, 1),
                    "implied_prob": round(implied_probability(wm_odds) * 100, 1),
                    "ev_percent": ev,
                    "commence_time": ref_event.get("commence_time", ""),
                    "num_books": ref_event.get("num_books", 1),
                    "match_id": wm_event.get("match_id", ""),
                    "start_time": wm_event.get("start_time", 0),
                })

    return value_bets


def _find_vb_ou(wm_events, ref_events, min_ev):
    """Trouve les value bets sur le marche Over/Under (tous seuils)."""
    value_bets = []

    # Grouper par seuil pour eviter qu'un O/U 1.5 WM matche un O/U 2.5 ref
    wm_by_threshold = {}
    for e in wm_events:
        t = e.get("market_threshold", 2.5)
        wm_by_threshold.setdefault(t, []).append(e)

    ref_by_threshold = {}
    for e in ref_events:
        t = e.get("market_threshold", 2.5)
        ref_by_threshold.setdefault(t, []).append(e)

    for threshold, wm_group in wm_by_threshold.items():
        ref_group = ref_by_threshold.get(threshold, [])
        if not ref_group:
            continue

        matches = match_events(wm_group, ref_group)

        for wm_event, ref_event in matches:
            ref_outcomes = devig_odds(ref_event.get("outcomes", []))
            fair_probs = {}
            for o in ref_outcomes:
                side = _normalize_ou_side(o["name"])
                if side:
                    fair_probs[side] = o["fair_prob"]

            for wm_outcome in wm_event.get("outcomes", []):
                side = _normalize_ou_side(wm_outcome["name"])
                if side not in fair_probs:
                    continue

                wm_odds = wm_outcome["odds"]
                best_prob = fair_probs[side]
                ev = calculate_ev(wm_odds, best_prob)

                if ev > min_ev and ev < 50:
                    value_bets.append({
                        "sport": wm_event.get("sport", ref_event.get("sport_title", "?")),
                        "home": wm_event.get("home", ref_event.get("home_team", "")),
                        "away": wm_event.get("away", ref_event.get("away_team", "")),
                        "market": f"over_under_{threshold}",
                        "market_type": "over_under",
                        "market_threshold": threshold,
                        "bet_on": wm_outcome["name"],
                        "winamax_odds": wm_odds,
                        "fair_prob": round(best_prob * 100, 1),
                        "implied_prob": round(implied_probability(wm_odds) * 100, 1),
                        "ev_percent": ev,
                        "commence_time": ref_event.get("commence_time", ""),
                        "num_books": ref_event.get("num_books", 1),
                        "match_id": wm_event.get("match_id", ""),
                        "start_time": wm_event.get("start_time", 0),
                    })

    return value_bets


def _find_vb_btts(wm_events, ref_events, min_ev):
    """Trouve les value bets sur le marche BTTS (Both Teams To Score)."""
    value_bets = []
    matches = match_events(wm_events, ref_events)

    for wm_event, ref_event in matches:
        ref_outcomes = devig_odds(ref_event.get("outcomes", []))
        fair_probs = {}
        for o in ref_outcomes:
            side = _normalize_btts_side(o["name"])
            if side:
                fair_probs[side] = o["fair_prob"]

        for wm_outcome in wm_event.get("outcomes", []):
            side = _normalize_btts_side(wm_outcome["name"])
            if side not in fair_probs:
                continue

            wm_odds = wm_outcome["odds"]
            best_prob = fair_probs[side]
            ev = calculate_ev(wm_odds, best_prob)

            if ev > min_ev and ev < 50:
                value_bets.append({
                    "sport": wm_event.get("sport", ref_event.get("sport_title", "?")),
                    "home": wm_event.get("home", ref_event.get("home_team", "")),
                    "away": wm_event.get("away", ref_event.get("away_team", "")),
                    "market": "btts",
                    "market_type": "btts",
                    "market_threshold": None,
                    "bet_on": wm_outcome["name"],
                    "winamax_odds": wm_odds,
                    "fair_prob": round(best_prob * 100, 1),
                    "implied_prob": round(implied_probability(wm_odds) * 100, 1),
                    "ev_percent": ev,
                    "commence_time": ref_event.get("commence_time", ""),
                    "num_books": ref_event.get("num_books", 1),
                    "match_id": wm_event.get("match_id", ""),
                    "start_time": wm_event.get("start_time", 0),
                })

    return value_bets


# ── Point d'entree principal ──

def find_value_bets(winamax_events, reference_events, min_ev=0.0):
    """
    Identifie les paris EV+ en comparant Winamax au consensus multi-books.
    Supporte h2h, over_under et btts.
    """
    # Separer par market_type
    wm_by_type = {}
    for e in winamax_events:
        wm_by_type.setdefault(_get_market_type(e), []).append(e)

    ref_by_type = {}
    for e in reference_events:
        ref_by_type.setdefault(_get_market_type(e), []).append(e)

    value_bets = []

    # H2H (inclut h2h_2way)
    wm_h2h = wm_by_type.get("h2h", []) + wm_by_type.get("h2h_2way", [])
    ref_h2h = ref_by_type.get("h2h", [])
    if wm_h2h and ref_h2h:
        value_bets.extend(_find_vb_h2h(wm_h2h, ref_h2h, min_ev))

    # Over/Under
    wm_ou = wm_by_type.get("over_under", [])
    ref_ou = ref_by_type.get("over_under", [])
    if wm_ou and ref_ou:
        value_bets.extend(_find_vb_ou(wm_ou, ref_ou, min_ev))

    # BTTS
    wm_btts = wm_by_type.get("btts", [])
    ref_btts = ref_by_type.get("btts", [])
    if wm_btts and ref_btts:
        value_bets.extend(_find_vb_btts(wm_btts, ref_btts, min_ev))

    value_bets.sort(key=lambda x: x["ev_percent"], reverse=True)
    return value_bets

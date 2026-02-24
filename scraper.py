"""
Module de scraping Winamax.
Utilise Selenium (Chrome headless) pour contourner la protection anti-bot,
puis extrait le PRELOADED_STATE depuis le HTML rendu.
Fallback sur The Odds API si Selenium echoue.
"""

import json
import re
import time
import requests

from config import ODDS_API_KEY, ODDS_API_BASE, ODDS_API_REGIONS

# Mapping des sports Winamax (IDs reels confirmes par scraping)
# ID Winamax -> (nom afiche, cle The Odds API)
WINAMAX_SPORTS = {
    1: ("Football", "soccer"),
    2: ("Basketball", "basketball"),
    3: ("Baseball", "baseball"),
    4: ("Hockey sur glace", "icehockey"),
    5: ("Tennis", "tennis"),
    6: ("Handball", "handball"),
    7: ("Volleyball", "volleyball"),
    8: ("Football americain", "americanfootball"),
    9: ("Golf", "golf"),
    10: ("Boxe", "boxing"),
    11: ("Automobile", "motorsport"),
    12: ("Rugby", "rugby_league"),
    13: ("MMA", "mma_mixed_martial_arts"),
    14: ("Cyclisme", "cycling"),
    30: ("Esport", "esports"),
}

# ── Selenium Chrome Driver ──

_driver = None


def _find_chromedriver():
    """Cherche le chromedriver deja installe, sinon installe via manager."""
    import os
    import glob as globmod

    # Chercher dans le cache webdriver-manager
    wdm_dir = os.path.join(os.path.expanduser("~"), ".wdm", "drivers", "chromedriver")
    if os.path.isdir(wdm_dir):
        pattern = os.path.join(wdm_dir, "**", "chromedriver.exe")
        found = globmod.glob(pattern, recursive=True)
        if found:
            # Prendre le plus recent
            found.sort(key=os.path.getmtime, reverse=True)
            print(f"[scraper] ChromeDriver cache trouve: {found[0]}")
            return found[0]

    # Pas de cache, on installe (lent mais necessaire la premiere fois)
    print("[scraper] Telechargement ChromeDriver...")
    from webdriver_manager.chrome import ChromeDriverManager
    return ChromeDriverManager().install()


def _get_driver():
    """Cree ou retourne le driver Chrome headless. Recrée si la session est morte."""
    global _driver
    if _driver is not None:
        # Vérifier que la session est encore vivante
        try:
            _ = _driver.current_url  # lève une exception si la session est morte
        except Exception:
            print("[scraper] Session Chrome morte, recréation...")
            try:
                _driver.quit()
            except Exception:
                pass
            _driver = None

    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.chrome.options import Options

        print("[scraper] Configuration Chrome...")

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        chromedriver_path = _find_chromedriver()
        print(f"[scraper] Lancement Chrome avec: {chromedriver_path}")

        service = Service(chromedriver_path)
        _driver = webdriver.Chrome(service=service, options=options)

        # Masquer la detection Selenium
        _driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = {runtime: {}};
            """
        })

        print("[scraper] Chrome headless demarre")
        return _driver

    except Exception as e:
        import traceback
        print(f"[scraper] Impossible de demarrer Chrome: {e}")
        traceback.print_exc()
        return None


def _fetch_page_selenium(url, wait_seconds=5):
    """Charge une page avec Selenium et retourne le HTML complet."""
    driver = _get_driver()
    if not driver:
        return None

    try:
        driver.get(url)
        time.sleep(wait_seconds)
        html = driver.page_source
        print(f"[scraper] Page chargee: {len(html)} chars")
        return html
    except Exception as e:
        print(f"[scraper] Erreur Selenium pour {url}: {e}")
        return None


def _scroll_and_collect(url, scroll_pause=2, max_scrolls=5):
    """Charge une page, scrolle pour le contenu dynamique."""
    driver = _get_driver()
    if not driver:
        return None

    try:
        driver.get(url)
        time.sleep(3)

        last_height = driver.execute_script("return document.body.scrollHeight")
        for i in range(max_scrolls):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(scroll_pause)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height

        return driver.page_source
    except Exception as e:
        print(f"[scraper] Erreur scroll {url}: {e}")
        return None


# ── Extraction des donnees ──

def _extract_preloaded_state(html):
    """Extrait le JSON PRELOADED_STATE depuis le HTML."""
    if not html:
        return None

    # Pattern principal
    pattern = r'var\s+PRELOADED_STATE\s*=\s*(\{.*?\})\s*;\s*var\s+BETTING_CONFIGURATION'
    match = re.search(pattern, html, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as e:
            print(f"[scraper] Erreur JSON PRELOADED_STATE: {e}")

    # Pattern alternatif
    pattern2 = r'PRELOADED_STATE\s*=\s*(\{.+?\})\s*;'
    match2 = re.search(pattern2, html, re.DOTALL)
    if match2:
        try:
            return json.loads(match2.group(1))
        except json.JSONDecodeError:
            pass

    return None


def _parse_state_data(state, sport_filter=None):
    """
    Parse le PRELOADED_STATE Winamax.

    Structure reelle confirmee :
    - state["sports"]    : {id: {sportName, categories, ...}}
    - state["matches"]   : {id: {title, sportId, matchStart, competitors?, ...}}
    - state["bets"]      : {id: {matchId, outcomes: [outcomeId, ...], label?}}
    - state["outcomes"]  : {id: {betId, label, ...}}  (PAS de cotes ici!)
    - state["odds"]      : {outcomeId: valeur_cote}   (cotes separees!)
    """
    if not state:
        return []

    matches = state.get("matches", {})
    bets = state.get("bets", {})
    outcomes_map = state.get("outcomes", {})
    odds_map = state.get("odds", {})  # Les cotes sont ICI, pas dans outcomes!
    sports = state.get("sports", {})

    events = []
    now = int(time.time())

    for match_id, match in matches.items():
        match_start = match.get("matchStart", match.get("startDate", 0))
        if isinstance(match_start, str):
            try:
                match_start = int(match_start)
            except ValueError:
                match_start = 0

        # Garder uniquement les matchs a venir (marge 1h pour live)
        if match_start > 0 and match_start < now - 3600:
            continue

        sport_id = match.get("sportId", 0)
        if sport_filter and sport_id != sport_filter:
            continue

        # Ignorer les paris outright (vainqueur competition)
        if match.get("isOutright", False):
            continue

        # Nom du sport
        sport_name = "?"
        sport_api_key = ""
        if sport_id in WINAMAX_SPORTS:
            sport_name, sport_api_key = WINAMAX_SPORTS[sport_id]
        elif str(sport_id) in sports:
            sport_info = sports[str(sport_id)]
            if isinstance(sport_info, dict):
                sport_name = sport_info.get("sportName", sport_info.get("label", "?"))

        # Noms des equipes
        title = match.get("title", "")
        competitors = match.get("competitors", [])

        home, away = "", ""
        if isinstance(competitors, list) and len(competitors) >= 2:
            if isinstance(competitors[0], dict):
                home = competitors[0].get("name", "")
                away = competitors[1].get("name", "")
            else:
                home = str(competitors[0])
                away = str(competitors[1])
        elif " - " in title:
            parts = title.split(" - ", 1)
            home, away = parts[0].strip(), parts[1].strip()
        elif " vs " in title.lower():
            parts = re.split(r'\s+vs\.?\s+', title, flags=re.IGNORECASE)
            home = parts[0].strip()
            away = parts[1].strip() if len(parts) > 1 else ""

        # Recuperer les paris pour ce match
        match_bets = []
        for bet_id, bet in bets.items():
            if not bet or not isinstance(bet, dict):
                continue
            if str(bet.get("matchId", "")) != str(match_id):
                continue

            # Les outcomes sont references par ID dans bet["outcomes"]
            outcome_ids = bet.get("outcomes", [])
            bet_outcomes = []

            for out_id in outcome_ids:
                out_id_str = str(out_id)
                outcome = outcomes_map.get(out_id_str) or outcomes_map.get(out_id)
                if not outcome or not isinstance(outcome, dict):
                    continue

                # Les cotes sont dans le dict odds_map, pas dans outcome
                odds_value = odds_map.get(out_id_str) or odds_map.get(out_id, 0)

                if isinstance(odds_value, str):
                    try:
                        odds_value = float(odds_value)
                    except ValueError:
                        continue
                elif not isinstance(odds_value, (int, float)):
                    continue

                # Winamax stocke parfois en centiemes (195 = 1.95)
                if odds_value > 100:
                    odds_value = odds_value / 100.0

                if odds_value <= 1.0:
                    continue

                label = outcome.get("label", outcome.get("name", "?"))
                bet_outcomes.append({
                    "name": label,
                    "odds": round(float(odds_value), 2),
                })

            if len(bet_outcomes) >= 2:
                # Nom du marche
                market_label = bet.get("label", bet.get("betType", ""))
                if not market_label:
                    # Determiner par le nombre d'issues
                    if len(bet_outcomes) == 2:
                        market_label = "1-2"
                    elif len(bet_outcomes) == 3:
                        market_label = "1X2"
                    else:
                        market_label = "Marche"

                match_bets.append({
                    "market": market_label,
                    "outcomes": bet_outcomes,
                })

        # Ajouter chaque marche comme evenement
        if match_bets:
            for bet_data in match_bets:
                mtype, mthreshold = _detect_market_type(bet_data["outcomes"])
                events.append({
                    "match_id": str(match_id),
                    "sport": sport_name,
                    "sport_api_key": sport_api_key,
                    "sport_id": sport_id,
                    "home": home,
                    "away": away,
                    "title": title,
                    "market": bet_data["market"],
                    "market_type": mtype,
                    "market_threshold": mthreshold,
                    "outcomes": bet_data["outcomes"],
                    "start_time": match_start,
                })

    return events


def _detect_market_type(outcomes):
    """
    Identifie le type de marche a partir des labels d'outcomes.
    Retourne (market_type, threshold) :
      - ("h2h", None)            : Resultat 1X2 / match nul
      - ("h2h_2way", None)       : Victoire / Defaite (sans nul)
      - ("over_under", 2.5)      : Plus/Moins de X buts
      - ("btts", None)           : Les deux equipes marquent
      - ("unknown", None)        : Non reconnu
    """
    if not outcomes:
        return "unknown", None

    labels = [o.get("name", "").lower() for o in outcomes]
    joined = " ".join(labels)

    # Over/Under : "plus de 2.5", "moins de 2.5", "over 2.5", "under 1.5"
    ou_keywords = ["plus de", "moins de", "over", "under", "+2.", "+1.", "+3.", "-2.", "-1.", "-3."]
    if any(kw in joined for kw in ou_keywords):
        m = re.search(r'(\d+[.,]\d+)', joined)
        threshold = float(m.group(1).replace(',', '.')) if m else 2.5
        return "over_under", threshold

    # BTTS : "les deux equipes marquent", "both teams to score"
    btts_keywords = ["deux equipes", "both teams", "btts", "les 2 equipes", "marquent"]
    if any(kw in joined for kw in btts_keywords):
        return "btts", None

    # 1X2 classique (3 outcomes : domicile / nul / exterieur)
    if len(outcomes) == 3:
        return "h2h", None

    # 2 outcomes : victoire/defaite sans nul (tennis, basket)
    if len(outcomes) == 2:
        return "h2h_2way", None

    return "unknown", None


# ── Fallback : The Odds API ──

def _get_winamax_odds_via_api():
    """Recupere les cotes via The Odds API (fallback)."""
    if not ODDS_API_KEY or ODDS_API_KEY == "YOUR_API_KEY_HERE":
        print("[scraper] Pas de cle API -- fallback impossible")
        return []

    print("[scraper] Fallback via The Odds API...")
    events = []

    try:
        resp = requests.get(
            f"{ODDS_API_BASE}/sports",
            params={"apiKey": ODDS_API_KEY},
            timeout=15,
        )
        resp.raise_for_status()
        api_sports = [s for s in resp.json() if s.get("active")]
    except Exception as e:
        print(f"[scraper] Erreur listing sports: {e}")
        return []

    for sport in api_sports:
        sport_key = sport["key"]
        try:
            resp = requests.get(
                f"{ODDS_API_BASE}/sports/{sport_key}/odds",
                params={
                    "apiKey": ODDS_API_KEY,
                    "regions": ODDS_API_REGIONS,
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                },
                timeout=15,
            )
            resp.raise_for_status()
            api_events = resp.json()
        except Exception as e:
            print(f"[scraper] Erreur cotes {sport_key}: {e}")
            continue

        for ev in api_events:
            winamax_book = None
            all_books = ev.get("bookmakers", [])

            for bm in all_books:
                if "winamax" in bm["key"].lower():
                    winamax_book = bm
                    break

            target_book = winamax_book or (all_books[0] if all_books else None)
            if not target_book:
                continue

            for market in target_book.get("markets", []):
                outcomes = [
                    {"name": o["name"], "odds": o["price"]}
                    for o in market.get("outcomes", [])
                    if o.get("price", 0) > 1.0
                ]

                if outcomes:
                    sport_api_key = sport_key.split("_")[0] if "_" in sport_key else sport_key
                    events.append({
                        "match_id": ev.get("id", ""),
                        "sport": sport.get("title", sport_key),
                        "sport_api_key": sport_api_key,
                        "sport_id": 0,
                        "home": ev.get("home_team", ""),
                        "away": ev.get("away_team", ""),
                        "title": f"{ev.get('home_team', '')} - {ev.get('away_team', '')}",
                        "market": market["key"],
                        "outcomes": outcomes,
                        "start_time": ev.get("commence_time", ""),
                        "source": target_book["title"],
                    })

        if api_events:
            print(f"[scraper] {sport['title']}: {len(api_events)} evenements via API")

    return events


# ── API publique ──

def get_sports():
    """Retourne la liste des sports disponibles."""
    return [
        {"id": sid, "name": name, "api_key": api_key}
        for sid, (name, api_key) in WINAMAX_SPORTS.items()
    ]


def get_all_events():
    """
    Recupere les evenements Winamax.
    1. Selenium (Chrome headless) pour scraper winamax.fr
    2. Fallback The Odds API si Chrome echoue
    """
    print("[scraper] Demarrage Selenium...")
    html = _scroll_and_collect(
        "https://www.winamax.fr/paris-sportifs/sports",
        scroll_pause=2,
        max_scrolls=3,
    )

    # Essayer PRELOADED_STATE
    state = _extract_preloaded_state(html)
    if state:
        events = _parse_state_data(state)
        if events:
            print(f"[scraper] {len(events)} evenements (Selenium)")
            return events

    # Tentative par sport
    print("[scraper] Tentative par sport individuel...")
    all_events = []
    for sport_id in WINAMAX_SPORTS:
        url = f"https://www.winamax.fr/paris-sportifs/sports/{sport_id}"
        html = _fetch_page_selenium(url, wait_seconds=4)
        state = _extract_preloaded_state(html)
        if state:
            evts = _parse_state_data(state, sport_filter=sport_id)
            if evts:
                sport_name = WINAMAX_SPORTS[sport_id][0]
                print(f"[scraper] {sport_name}: {len(evts)} evenements")
                all_events.extend(evts)

    if all_events:
        print(f"[scraper] {len(all_events)} evenements total")
        return all_events

    # Fallback The Odds API
    print("[scraper] Selenium n'a pas trouve de donnees, fallback API...")
    return _get_winamax_odds_via_api()


def get_match_result(match_id, home="", away="", start_time=0, sport="Football"):
    """
    Recupere le resultat d'un match termine.
    1. Essaie Winamax (Selenium + PRELOADED_STATE)
    2. Fallback ESPN API (sans auth, gratuit)

    Returns:
        dict {"status": "finished", "score": "3-1",
              "winning_outcomes": ["PSG"], "home": str, "away": str}
        ou None si pas encore termine / introuvable.
    """
    # ── 1. Winamax via Selenium ──
    result = _get_result_winamax(match_id)
    if result is not None:
        return result

    # ── 2. Fallback ESPN API ──
    if home and away and start_time:
        result = _get_result_espn(home, away, start_time, sport)
        if result is not None:
            print(f"[scraper] Resultat ESPN: {home} vs {away} -> {result.get('score')}")
            return result

    return None


def _get_result_winamax(match_id):
    """Cherche le resultat sur la page Winamax du match."""
    url = f"https://www.winamax.fr/paris-sportifs/match/{match_id}"
    html = _fetch_page_selenium(url, wait_seconds=4)
    state = _extract_preloaded_state(html)

    if not state:
        return None

    matches = state.get("matches", {})
    match = matches.get(str(match_id)) or matches.get(match_id)

    if not match:
        for mid, m in matches.items():
            if str(mid) == str(match_id):
                match = m
                break

    if not match:
        return None

    is_finished = match.get("isFinished", False)
    is_live = match.get("isLive", False)
    status_raw = str(match.get("status", match.get("matchStatus", "")))

    finished_statuses = {"FINISHED", "FT", "ENDED", "TERMINATED", "3", "CLOSED", "RESOLVED"}
    if not is_finished and status_raw.upper() not in finished_statuses:
        if is_live:
            return {"match_id": str(match_id), "status": "live",
                    "score": "", "winning_outcomes": [], "home": "", "away": ""}
        return None

    title = match.get("title", "")
    competitors = match.get("competitors", [])
    home, away = "", ""
    if isinstance(competitors, list) and len(competitors) >= 2:
        if isinstance(competitors[0], dict):
            home = competitors[0].get("name", "")
            away = competitors[1].get("name", "")
        else:
            home, away = str(competitors[0]), str(competitors[1])
    elif " - " in title:
        parts = title.split(" - ", 1)
        home, away = parts[0].strip(), parts[1].strip()

    scoreboard = match.get("scoreboard", match.get("score", match.get("scores", {})))
    home_score, away_score = None, None

    if isinstance(scoreboard, dict):
        home_score = scoreboard.get("home", scoreboard.get("1", scoreboard.get("s1")))
        away_score = scoreboard.get("away", scoreboard.get("2", scoreboard.get("s2")))
    elif isinstance(scoreboard, list) and len(scoreboard) >= 2:
        home_score, away_score = scoreboard[0], scoreboard[1]

    if home_score is None and isinstance(competitors, list):
        for i, c in enumerate(competitors):
            if isinstance(c, dict) and "score" in c:
                if i == 0:
                    home_score = c["score"]
                elif i == 1:
                    away_score = c["score"]

    winning_outcomes = []
    score_str = ""

    if home_score is not None and away_score is not None:
        try:
            hs, as_ = int(home_score), int(away_score)
            score_str = f"{hs}-{as_}"
            if hs > as_:
                winning_outcomes = [home]
            elif as_ > hs:
                winning_outcomes = [away]
            else:
                winning_outcomes = ["Draw", "Match nul", "Nul", "X"]
        except (ValueError, TypeError):
            score_str = f"{home_score}-{away_score}"

    if not winning_outcomes:
        bets_data = state.get("bets", {})
        outcomes_data = state.get("outcomes", {})
        for bid, bet in bets_data.items():
            if not bet or not isinstance(bet, dict):
                continue
            if str(bet.get("matchId", "")) != str(match_id):
                continue
            for out_id in bet.get("outcomes", []):
                outcome = outcomes_data.get(str(out_id), {})
                if not isinstance(outcome, dict):
                    continue
                if outcome.get("isWinning") or outcome.get("result") in ("WON", "WIN", "1"):
                    label = outcome.get("label", outcome.get("name", ""))
                    if label:
                        winning_outcomes.append(label)

    print(f"[scraper] Winamax {match_id}: {home} {score_str} {away} -> {winning_outcomes}")
    return {
        "match_id": str(match_id),
        "status": "finished",
        "score": score_str,
        "winning_outcomes": winning_outcomes,
        "home": home,
        "away": away,
    }


# Leagues ESPN par sport Winamax
_ESPN_LEAGUES = {
    "Football": [
        "ita.1", "esp.1", "eng.1", "ger.1", "fra.1", "por.1", "ned.1",
        "tur.1", "bel.1", "sco.1", "ita.2", "esp.2", "eng.2", "ger.2", "fra.2",
        "uefa.champions", "uefa.europa", "uefa.europa_conference",
        "eng.fa", "ger.dfb_pokal", "esp.copa_del_rey", "ita.coppa_italia",
        "fra.coupe_de_france", "eng.league_cup",
    ],
    "Basketball": ["nba", "eur.1", "esp.1", "ita.1", "fra.1"],
    "Tennis": [],
}


# Noms français/alternatifs → nom anglais officiel
_TEAM_TRANSLATIONS = {
    # Espagne
    "gérone": "girona", "gerone": "girona",
    "séville": "sevilla", "seville": "sevilla",
    "valence": "valencia",
    "betis": "real betis", "betis seville": "real betis",
    "la corogne": "deportivo",
    "saragosse": "zaragoza",
    "majorque": "mallorca",
    "real societe": "real sociedad", "real société": "real sociedad",
    "osasune": "osasuna",
    "espagnol": "espanyol",
    # Angleterre
    "manchester city": "man city",
    "manchester united": "man united", "manchester utd": "man utd",
    "newcastle united": "newcastle",
    "west ham united": "west ham",
    "tottenham hotspur": "tottenham", "spurs": "tottenham",
    "leicester city": "leicester",
    "brighton & hove albion": "brighton",
    "wolverhampton": "wolves",
    # Allemagne
    "munich": "bayern munich", "bayern": "bayern munich",
    "leverkusen": "bayer leverkusen",
    "dortmund": "borussia dortmund",
    "gladbach": "m'gladbach",
    "cologne": "koln", "köln": "koln",
    "mayence": "mainz", "mayence 05": "mainz",
    "francfort": "frankfurt", "eintracht francfort": "eintracht frankfurt",
    "stuttgart vfb": "vfb stuttgart",
    "fribourg": "freiburg", "sc fribourg": "sc freiburg",
    "hertha berlin": "hertha",
    "union berlin": "union",
    # Italie
    "inter milan": "inter", "internazionale": "inter",
    "ac milan": "milan",
    "juventus turin": "juventus",
    "rome": "roma", "as rome": "roma",
    "naples": "napoli",
    "florence": "fiorentina",
    "atalante": "atalanta",
    # France
    "paris": "paris saint-germain", "psg": "paris saint-germain",
    "saint-etienne": "saint-etienne",
    "marseille": "olympique marseille",
    "lyon": "olympique lyonnais",
    "bordeaux": "girondins bordeaux",
    "strasbourg": "rc strasbourg",
    "lens": "rc lens",
    # Portugal
    "porto": "fc porto",
    "sporting": "sporting cp", "sporting lisbonne": "sporting cp",
    "benfica": "sl benfica",
    "braga": "sc braga",
    # Pays-Bas
    "ajax": "ajax amsterdam",
    "psv": "psv eindhoven",
    "feyenoord": "feyenoord rotterdam",
}


def _normalize_team(name):
    """Normalise un nom d'equipe pour la comparaison."""
    import unicodedata
    name = name.lower().strip()
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    for word in ["fc", "ac", "as", "sc", "us", "ss", "rc", "og", "afc", "cf", "cd"]:
        name = re.sub(r'\b' + word + r'\b', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return _TEAM_TRANSLATIONS.get(name, name)


def _teams_match(name1, name2):
    """Verifie si deux noms d'equipe correspondent (fuzzy)."""
    from difflib import SequenceMatcher
    n1, n2 = _normalize_team(name1), _normalize_team(name2)
    if n1 == n2:
        return True
    if n1 in n2 or n2 in n1:
        return True
    ratio = SequenceMatcher(None, n1, n2).ratio()
    return ratio >= 0.65


def _get_result_espn(home, away, start_time, sport):
    """
    Cherche le score via ESPN unofficial API.
    Essaie plusieurs leagues jusqu'a trouver le match.
    """
    import urllib.request as ur
    import datetime

    date_str = datetime.datetime.utcfromtimestamp(start_time).strftime("%Y%m%d")
    leagues = _ESPN_LEAGUES.get(sport, _ESPN_LEAGUES.get("Football", []))

    for league in leagues:
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard?dates={date_str}"
        try:
            req = ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = ur.urlopen(req, timeout=6)
            data = json.loads(resp.read())
        except Exception:
            continue

        for event in data.get("events", []):
            comp = event.get("competitions", [{}])[0]
            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue

            espn_home = competitors[0].get("team", {}).get("displayName", "")
            espn_away = competitors[1].get("team", {}).get("displayName", "")

            # Matcher les equipes (dans les deux sens)
            home_ok = _teams_match(home, espn_home) or _teams_match(home, espn_away)
            away_ok = _teams_match(away, espn_home) or _teams_match(away, espn_away)
            if not (home_ok and away_ok):
                continue

            status = comp.get("status", {})
            if not status.get("type", {}).get("completed", False):
                state_str = status.get("type", {}).get("state", "")
                if state_str == "in":
                    return {"match_id": "", "status": "live", "score": "", "winning_outcomes": [], "home": home, "away": away}
                return None

            # Match fini
            # ESPN: competitors[0] = home, competitors[1] = away
            # mais l'ordre peut etre inverse si Winamax a home/away dans l'autre sens
            scores = {c["team"]["displayName"]: int(c.get("score", 0)) for c in competitors}
            home_key = next((k for k in scores if _teams_match(home, k)), None)
            away_key = next((k for k in scores if _teams_match(away, k)), None)

            if home_key and away_key:
                hs, as_ = scores[home_key], scores[away_key]
            else:
                hs = int(competitors[0].get("score", 0))
                as_ = int(competitors[1].get("score", 0))
                home_key, away_key = espn_home, espn_away

            score_str = f"{hs}-{as_}"
            if hs > as_:
                winning_outcomes = [home]
            elif as_ > hs:
                winning_outcomes = [away]
            else:
                winning_outcomes = ["Draw", "Match nul", "Nul", "X"]

            return {
                "match_id": "",
                "status": "finished",
                "score": score_str,
                "winning_outcomes": winning_outcomes,
                "home": home,
                "away": away,
            }

    return None


def get_match_results_batch(match_ids):
    """
    Recupere les resultats de plusieurs matchs en batch.
    Charge d'abord la page sports principale, puis fallback individuel.

    Returns:
        dict match_id -> result dict
    """
    results = {}
    remaining = set(str(mid) for mid in match_ids)

    # Essayer la page sports principale d'abord
    html = _fetch_page_selenium(
        "https://www.winamax.fr/paris-sportifs/sports",
        wait_seconds=4,
    )
    state = _extract_preloaded_state(html)

    if state:
        matches_data = state.get("matches", {})
        for mid in list(remaining):
            match = matches_data.get(mid)
            if not match:
                continue
            is_finished = match.get("isFinished", False)
            status_raw = str(match.get("status", ""))
            if is_finished or status_raw.upper() in ("FINISHED", "FT", "ENDED", "3", "CLOSED"):
                result = get_match_result(mid)
                if result and result["status"] == "finished":
                    results[mid] = result
                    remaining.discard(mid)

    # Fallback: pages individuelles (max 5)
    for mid in list(remaining)[:5]:
        result = get_match_result(mid)
        if result and result["status"] == "finished":
            results[mid] = result

    return results


def cleanup():
    """Ferme le driver Chrome proprement."""
    global _driver
    if _driver:
        try:
            _driver.quit()
        except Exception:
            pass
        _driver = None

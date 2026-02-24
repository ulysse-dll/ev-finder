"""
Module de recuperation des cotes de reference via Oddsportal.
Scrape les cotes multi-bookmakers (gratuit, illimite).
Utilise Selenium pour extraire les donnees du DOM rendu.
"""

import re
import time

_driver = None


def _get_driver():
    """Cree ou reutilise un driver Chrome headless."""
    global _driver
    if _driver is not None:
        try:
            _driver.current_url
            return _driver
        except Exception:
            _driver = None

    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.chrome.options import Options
        from webdriver_manager.chrome import ChromeDriverManager

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--window-size=1920,1080")
        options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        service = Service(ChromeDriverManager().install())
        _driver = webdriver.Chrome(service=service, options=options)
        _driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        })
        print("[odds] Chrome headless demarre (reference)")
        return _driver
    except Exception as e:
        print(f"[odds] Impossible de demarrer Chrome: {e}")
        return None


def _scrape_oddsportal_page(url, sport_name):
    """
    Scrape une page Oddsportal via JS pour extraire les lignes de matchs.
    Chaque ligne contient: heure, equipe1, equipe2, cote1, coteX, cote2, nb_books.
    """
    driver = _get_driver()
    if not driver:
        return []

    events = []
    try:
        driver.get(url)
        time.sleep(5)

        # Extraire les lignes de matchs via JS
        rows = driver.execute_script('''
            // Trouver toutes les lignes qui ressemblent a des matchs
            const allEls = document.querySelectorAll(
                "div[class*='flex'][class*='border'], " +
                "div[class*='eventRow'], " +
                "a[href*='/match/']"
            );
            const results = [];
            const seen = new Set();

            allEls.forEach(el => {
                const text = el.innerText.trim();
                // Un match a typiquement: heure, 2 equipes, 2-3 cotes
                const lines = text.split('\\n').map(s => s.trim()).filter(s => s);
                if (lines.length < 4) return;

                // Chercher un pattern: des nombres decimaux (cotes)
                const odds = lines.filter(l => /^\d+\.\d{2}$/.test(l));
                // Chercher des noms (pas des nombres, pas trop courts)
                const names = lines.filter(l => l.length > 2 && !/^\d/.test(l) && !/^[XB]/.test(l));

                if (odds.length >= 2 && names.length >= 2) {
                    const key = names[0] + '_' + names[1];
                    if (!seen.has(key)) {
                        seen.add(key);
                        // Chercher le nombre de bookmakers (dernier nombre entier)
                        const numBooks = lines.filter(l => /^\d+$/.test(l) && parseInt(l) > 3 && parseInt(l) < 100);
                        results.push({
                            home: names[0],
                            away: names[1],
                            odds: odds.map(parseFloat),
                            numBooks: numBooks.length > 0 ? parseInt(numBooks[numBooks.length - 1]) : 0
                        });
                    }
                }
            });
            return results;
        ''')

        if not rows:
            # Fallback: extraction via innerText global
            rows = _fallback_extraction(driver)

        for row in rows:
            home = row.get("home", "")
            away = row.get("away", "")
            odds_list = row.get("odds", [])
            num_books = row.get("numBooks", 0)

            if not home or not away or len(odds_list) < 2:
                continue

            # Construire les outcomes
            outcomes = [{"name": home, "odds": odds_list[0]}]

            if len(odds_list) == 3:
                # 3-way: 1X2 (football, handball, hockey)
                outcomes.append({"name": "Draw", "odds": odds_list[1]})
                outcomes.append({"name": away, "odds": odds_list[2]})
            elif len(odds_list) == 2:
                # 2-way: H2H (tennis, basketball, MMA)
                outcomes.append({"name": away, "odds": odds_list[1]})

            # Filtrer les cotes invalides
            valid = [o for o in outcomes if o["odds"] > 1.0]
            if len(valid) >= 2:
                events.append({
                    "event_id": "",
                    "sport_title": sport_name,
                    "home_team": home,
                    "away_team": away,
                    "commence_time": "",
                    "market": "h2h",
                    "outcomes": valid,
                    "num_books": num_books,
                    "source": "oddsportal",
                })

        print(f"[odds] {sport_name}: {len(events)} evenements")

    except Exception as e:
        print(f"[odds] Erreur scraping {url}: {e}")

    return events


def _fallback_extraction(driver):
    """Extraction fallback via le texte brut de la page."""
    try:
        text = driver.execute_script("return document.body.innerText;")
        lines = text.split("\n")

        rows = []
        i = 0
        while i < len(lines) - 5:
            line = lines[i].strip()
            # Chercher un pattern horaire (HH:MM)
            if re.match(r'^\d{2}:\d{2}$', line):
                # Les lignes suivantes devraient etre: equipe1, score1?, equipe2, score2?, cotes...
                remaining = []
                for j in range(1, 10):
                    if i + j < len(lines):
                        remaining.append(lines[i + j].strip())

                names = [r for r in remaining if len(r) > 2 and not re.match(r'^[\d.]+$', r)
                         and r not in ('X', "B's", '1', '2')]
                odds = [float(r) for r in remaining if re.match(r'^\d+\.\d{2}$', r)]

                if len(names) >= 2 and len(odds) >= 2:
                    num_books = [int(r) for r in remaining
                                 if re.match(r'^\d+$', r) and 3 < int(r) < 100]
                    rows.append({
                        "home": names[0],
                        "away": names[1],
                        "odds": odds[:3],
                        "numBooks": num_books[-1] if num_books else 0,
                    })
                    i += 6
                    continue
            i += 1

        return rows
    except Exception:
        return []


# URLs Oddsportal par sport
ODDSPORTAL_URLS = {
    "soccer": ("Football", [
        "https://www.oddsportal.com/football/england/premier-league/",
        "https://www.oddsportal.com/football/france/ligue-1/",
        "https://www.oddsportal.com/football/spain/laliga/",
        "https://www.oddsportal.com/football/germany/bundesliga/",
        "https://www.oddsportal.com/football/italy/serie-a/",
        "https://www.oddsportal.com/football/europe/champions-league/",
    ]),
    "tennis": ("Tennis", [
        "https://www.oddsportal.com/tennis/",
    ]),
    "basketball": ("Basketball", [
        "https://www.oddsportal.com/basketball/usa/nba/",
        "https://www.oddsportal.com/basketball/europe/euroleague/",
    ]),
    "icehockey": ("Hockey", [
        "https://www.oddsportal.com/hockey/usa/nhl/",
    ]),
    "handball": ("Handball", [
        "https://www.oddsportal.com/handball/",
    ]),
    "rugby_league": ("Rugby", [
        "https://www.oddsportal.com/rugby-league/",
    ]),
    "mma_mixed_martial_arts": ("MMA", [
        "https://www.oddsportal.com/mma/",
    ]),
    "boxing": ("Boxe", [
        "https://www.oddsportal.com/boxing/",
    ]),
}


def get_available_sports():
    """Liste des sports disponibles."""
    return [
        {"key": key, "title": name, "active": True}
        for key, (name, urls) in ODDSPORTAL_URLS.items()
    ]


def _scrape_oddsportal_ou_page(url, sport_name, threshold=2.5):
    """
    Scrape l'onglet Over/Under d'une page Oddsportal football.
    Navigue vers {url}#over-under;2, clique sur le seuil voulu (1.5/2.5/3.5),
    puis extrait les cotes Over/Under.

    Retourne une liste d'events avec market_type="over_under".
    """
    driver = _get_driver()
    if not driver:
        return []

    # Oddsportal charge l'onglet O/U via le hash fragment
    ou_url = url.rstrip("/") + "/#over-under;2"
    events = []
    threshold_label = str(threshold)  # "1.5", "2.5", "3.5"

    try:
        driver.get(ou_url)
        time.sleep(5)

        # Cliquer sur l'onglet "Over/Under" principal si besoin
        try:
            from selenium.webdriver.common.by import By
            tabs = driver.find_elements(By.XPATH,
                "//*[contains(text(),'Over/Under') or contains(text(),'Goals') or contains(text(),'Buts')]")
            if tabs:
                tabs[0].click()
                time.sleep(2)
        except Exception:
            pass

        # Cliquer sur le seuil specifique (1.5 / 2.5 / 3.5)
        try:
            clicked = driver.execute_script(f"""
                const label = '{threshold_label}';
                const candidates = Array.from(document.querySelectorAll(
                    'button, [role="tab"], [class*="tab"], [class*="filter"], [class*="btn"], a, li, span'
                ));
                const target = candidates.find(el =>
                    el.children.length === 0 && el.innerText.trim() === label
                );
                if (target) {{ target.click(); return true; }}
                return false;
            """)
            if clicked:
                time.sleep(2)
        except Exception:
            pass

        rows = driver.execute_script('''
            const allEls = document.querySelectorAll(
                "div[class*='flex'][class*='border'], " +
                "div[class*='eventRow'], " +
                "a[href*='/match/']"
            );
            const results = [];
            const seen = new Set();

            allEls.forEach(el => {
                const text = el.innerText.trim();
                const lines = text.split("\\n").map(s => s.trim()).filter(s => s);
                if (lines.length < 4) return;

                const odds = lines.filter(l => /^\\d+\\.\\d{2}$/.test(l));
                const names = lines.filter(l => l.length > 2 && !/^\\d/.test(l));

                if (odds.length >= 2 && names.length >= 2) {
                    const key = names[0] + "_" + names[1];
                    if (!seen.has(key)) {
                        seen.add(key);
                        const numBooks = lines.filter(l => /^\\d+$/.test(l) && parseInt(l) > 3 && parseInt(l) < 100);
                        results.push({
                            home: names[0],
                            away: names[1],
                            odds: odds.map(parseFloat).slice(0, 2),
                            numBooks: numBooks.length > 0 ? parseInt(numBooks[numBooks.length - 1]) : 0
                        });
                    }
                }
            });
            return results;
        ''')

        for row in (rows or []):
            home = row.get("home", "")
            away = row.get("away", "")
            odds_list = row.get("odds", [])
            num_books = row.get("numBooks", 0)

            if not home or not away or len(odds_list) < 2:
                continue

            events.append({
                "event_id": "",
                "sport_title": sport_name,
                "home_team": home,
                "away_team": away,
                "commence_time": "",
                "market": f"over_under_{threshold}",
                "market_type": "over_under",
                "market_threshold": threshold,
                "outcomes": [
                    {"name": f"Over {threshold}", "odds": odds_list[0]},
                    {"name": f"Under {threshold}", "odds": odds_list[1]},
                ],
                "num_books": num_books,
                "source": "oddsportal",
            })

        print(f"[odds] {sport_name} O/U{threshold}: {len(events)} evenements")

    except Exception as e:
        print(f"[odds] Erreur O/U {url}: {e}")

    return events


def _scrape_oddsportal_btts_page(url, sport_name):
    """
    Scrape l'onglet BTTS (Both Teams To Score) d'une page Oddsportal football.
    Navigue vers {url}#bts;2 et extrait les cotes Oui/Non.
    """
    driver = _get_driver()
    if not driver:
        return []

    btts_url = url.rstrip("/") + "/#bts;2"
    events = []

    try:
        driver.get(btts_url)
        time.sleep(5)

        try:
            from selenium.webdriver.common.by import By
            tabs = driver.find_elements(By.XPATH,
                "//*[contains(text(),'Both Teams') or contains(text(),'BTTS') or contains(text(),'Marquent')]")
            if tabs:
                tabs[0].click()
                time.sleep(3)
        except Exception:
            pass

        rows = driver.execute_script('''
            const allEls = document.querySelectorAll(
                "div[class*='flex'][class*='border'], div[class*='eventRow'], a[href*='/match/']"
            );
            const results = [];
            const seen = new Set();
            allEls.forEach(el => {
                const lines = el.innerText.trim().split("\\n").map(s => s.trim()).filter(s => s);
                if (lines.length < 4) return;
                const odds = lines.filter(l => /^\\d+\\.\\d{2}$/.test(l));
                const names = lines.filter(l => l.length > 2 && !/^\\d/.test(l));
                if (odds.length >= 2 && names.length >= 2) {
                    const key = names[0] + "_" + names[1];
                    if (!seen.has(key)) {
                        seen.add(key);
                        const nb = lines.filter(l => /^\\d+$/.test(l) && parseInt(l) > 3 && parseInt(l) < 100);
                        results.push({ home: names[0], away: names[1],
                            odds: odds.map(parseFloat).slice(0,2),
                            numBooks: nb.length ? parseInt(nb[nb.length-1]) : 0 });
                    }
                }
            });
            return results;
        ''')

        for row in (rows or []):
            home = row.get("home", "")
            away = row.get("away", "")
            odds_list = row.get("odds", [])
            num_books = row.get("numBooks", 0)

            if not home or not away or len(odds_list) < 2:
                continue

            events.append({
                "event_id": "",
                "sport_title": sport_name,
                "home_team": home,
                "away_team": away,
                "commence_time": "",
                "market": "btts",
                "market_type": "btts",
                "market_threshold": None,
                "outcomes": [
                    {"name": "Oui", "odds": odds_list[0]},
                    {"name": "Non", "odds": odds_list[1]},
                ],
                "num_books": num_books,
                "source": "oddsportal",
            })

        print(f"[odds] {sport_name} BTTS: {len(events)} evenements")

    except Exception as e:
        print(f"[odds] Erreur BTTS {url}: {e}")

    return events


def get_reference_odds(sport_key, markets="h2h"):
    """
    Recupere les cotes multi-bookmakers pour un sport.
    markets peut etre "h2h", "over_under", "btts" ou "all".
    """
    # Matcher par prefixe (soccer_epl -> soccer)
    base_key = sport_key
    if base_key not in ODDSPORTAL_URLS:
        base_key = sport_key.split("_")[0]
    if base_key not in ODDSPORTAL_URLS:
        return []

    sport_name, urls = ODDSPORTAL_URLS[base_key]
    all_events = []

    for url in urls:
        # H2H toujours
        if markets in ("h2h", "all"):
            events = _scrape_oddsportal_page(url, sport_name)
            all_events.extend(events)
            time.sleep(1)

        # Over/Under (football seulement) — seuils 1.5, 2.5, 3.5
        if markets in ("over_under", "all") and base_key == "soccer":
            for threshold in [1.5, 2.5, 3.5]:
                events = _scrape_oddsportal_ou_page(url, sport_name, threshold=threshold)
                all_events.extend(events)
                time.sleep(1)

        # BTTS (football seulement)
        if markets in ("btts", "all") and base_key == "soccer":
            events = _scrape_oddsportal_btts_page(url, sport_name)
            all_events.extend(events)
            time.sleep(1)

    return all_events


def get_all_reference_odds():
    """Recupere les cotes pour tous les sports."""
    all_odds = {}
    for key in ODDSPORTAL_URLS:
        odds = get_reference_odds(key)
        if odds:
            all_odds[key] = odds
    return all_odds


def cleanup():
    """Ferme le driver Chrome."""
    global _driver
    if _driver:
        try:
            _driver.quit()
        except Exception:
            pass
        _driver = None

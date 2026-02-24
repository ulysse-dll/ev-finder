"""
Module de recuperation des cotes de reference via Oddsportal.
Approche en deux phases :
  Phase 1 (overview page) : extraire la liste des matchs + URLs individuels
  Phase 2 (match page)    : visiter chaque match et extraire uniquement les
                            cotes Pinnacle (ou Betfair en fallback)

Pourquoi Pinnacle ?
  Les books "soft" (Bet365, Unibet...) ont une marge de 8-10%.
  Pinnacle a ~2% de marge et est la reference industrielle pour le "vrai prix".
  Devigged Pinnacle = meilleure approximation de la vraie probabilite.
"""

import re
import time

_driver = None

# Cache des stubs de matchs par URL de league (evite de re-scraper l'overview)
_match_stub_cache = {}
_match_stub_cache_ts = {}
STUB_CACHE_TTL = 600  # 10 minutes


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

        import tempfile, os as _os
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
        # Repertoire de profil unique pour eviter les conflits avec l'instance Winamax
        _ud = _os.path.join(tempfile.gettempdir(), "ev_odds_chrome")
        options.add_argument(f"--user-data-dir={_ud}")
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


# ── Phase 1 : extraction des stubs depuis la page overview ──

def _extract_match_stubs(driver, url):
    """
    Scrape la page overview Oddsportal pour recuperer la liste des matchs.
    Retourne une liste de dicts { home, away, match_url }.

    Note : Oddsportal N'utilise PAS /match/ dans ses URLs.
    Les matchs ont la forme /sport/pays/ligue/equipe1-equipe2-matchid/
    soit exactement UN segment de plus que la page ligue courante.
    """
    try:
        driver.get(url)
        time.sleep(7)  # Laisser React/JS rendre le contenu

        title = driver.execute_script("return document.title")
        print(f"[odds] Page chargee: {title[:70] if title else 'N/A'}")

        stubs = driver.execute_script('''
            const results = [];
            const seen = new Set();
            const BASE = "https://www.oddsportal.com";

            // Profondeur de la page courante (ex: /football/england/premier-league/ = 3)
            const baseParts = window.location.pathname.split("/").filter(Boolean);
            const baseDepth = baseParts.length;

            // Les pages de matchs ont EXACTEMENT un segment de plus
            // Ex: /football/england/premier-league/arsenal-chelsea-xAb123/ = profondeur 4
            const links = document.querySelectorAll("a[href]");

            links.forEach(link => {
                const href = link.getAttribute("href");
                if (!href || !href.startsWith("/") || href.includes("#")) return;

                const parts = href.split("/").filter(Boolean);
                if (parts.length !== baseDepth + 1) return;

                // Le dernier segment doit ressembler a un slug de match (avec tirets, min 5 chars)
                const slug = parts[parts.length - 1];
                if (!slug.includes("-") || slug.length < 5) return;
                // Eviter les liens vers des archives ou resultats
                if (href.includes("results") || href.includes("archive")) return;

                // Remonter dans le DOM pour trouver la ligne du match
                let container = link;
                for (let i = 0; i < 6; i++) {
                    if (!container.parentElement) break;
                    container = container.parentElement;
                    const cls = container.className || "";
                    if (cls.includes("flex") || cls.includes("border") ||
                        container.tagName === "TR") break;
                }

                const text = container.innerText || link.innerText || "";
                const lines = text.split("\\n")
                    .map(s => s.trim())
                    .filter(s => s.length > 2);

                // Garder uniquement les lignes qui ressemblent a des noms d'equipes
                const SKIP = new Set(["X", "1", "2", "Draw", "Nul", "N/A", "-", "+"]);
                const names = lines.filter(l =>
                    !SKIP.has(l) &&
                    !/^[\\d:.+\\-\\/]+$/.test(l) &&
                    !/^\\d+\\.\\d+$/.test(l) &&
                    l.length > 2 && l.length < 60
                );

                if (names.length >= 2) {
                    const key = names[0] + "||" + names[1];
                    if (!seen.has(key)) {
                        seen.add(key);
                        results.push({
                            home: names[0],
                            away: names[1],
                            match_url: BASE + href
                        });
                    }
                }
            });

            return results;
        ''')

        stubs = stubs or []
        print(f"[odds] Overview {url.split('/')[-2] or url}: {len(stubs)} matchs trouves")
        return stubs

    except Exception as e:
        print(f"[odds] Erreur overview {url}: {e}")
        return []


def _get_match_stubs(driver, url):
    """Retourne les stubs depuis le cache ou en les scrapant."""
    now = time.time()
    if url in _match_stub_cache and (now - _match_stub_cache_ts.get(url, 0)) < STUB_CACHE_TTL:
        return _match_stub_cache[url]

    stubs = _extract_match_stubs(driver, url)
    _match_stub_cache[url] = stubs
    _match_stub_cache_ts[url] = now
    return stubs


# ── Phase 2 : extraction des cotes Pinnacle depuis la page du match ──

def _scrape_sharp_odds_from_match_page(driver, match_url, market="h2h", threshold=None):
    """
    Navigue vers la page individuelle du match et extrait les cotes
    Pinnacle (ou Betfair Exchange en fallback).

    market : "h2h", "over_under", "btts"
    threshold : pour over_under, le seuil (1.5, 2.5, 3.5)

    Retourne { book: str, odds: [float, ...] } ou None si aucun book sharp trouve.
    """
    # Construire l'URL avec le bon fragment
    if market == "over_under":
        nav_url = match_url.rstrip("/") + "/#over-under;2"
    elif market == "btts":
        nav_url = match_url.rstrip("/") + "/#bts;2"
    else:
        nav_url = match_url

    try:
        driver.get(nav_url)
        time.sleep(4)

        # Pour O/U, cliquer sur le bon seuil
        if market == "over_under" and threshold:
            threshold_label = str(threshold)
            try:
                driver.execute_script(f"""
                    const label = '{threshold_label}';
                    const candidates = Array.from(document.querySelectorAll(
                        'button, [role="tab"], [class*="tab"], [class*="filter"], ' +
                        '[class*="btn"], a, li, span'
                    ));
                    const target = candidates.find(el =>
                        el.children.length === 0 && el.innerText.trim() === label
                    );
                    if (target) target.click();
                """)
                time.sleep(1.5)
            except Exception:
                pass

        result = driver.execute_script('''
            const SHARP_BOOKS = ["Pinnacle", "Betfair"];
            const PRIORITY = {"Pinnacle": 0, "Betfair": 1};
            const found = [];

            // Chercher dans le DOM les lignes contenant un bookmaker sharp
            const selectors = [
                "div[class*='border-b']",
                "tr",
                "div[class*='flex'][class*='items']",
                "div[class*='bookmaker']",
                "div[class*='odd-']",
                "div[class*='table-row']",
            ];
            const allRows = document.querySelectorAll(selectors.join(", "));

            for (const row of allRows) {
                const text = row.innerText || "";
                const imgs = Array.from(row.querySelectorAll("img"));

                for (const book of SHARP_BOOKS) {
                    const hasText = text.includes(book);
                    const hasImg = imgs.some(img =>
                        (img.alt || "").toLowerCase().includes(book.toLowerCase()) ||
                        (img.src || "").toLowerCase().includes(book.toLowerCase())
                    );

                    if (!hasText && !hasImg) continue;

                    // Extraire tous les decimaux de cette ligne
                    const allNums = text.split("\\n")
                        .map(s => s.trim())
                        .filter(s => /^\\d+\\.\\d{2}$/.test(s))
                        .map(parseFloat)
                        .filter(n => n > 1.01 && n < 50);

                    if (allNums.length >= 2) {
                        // Prendre les 2 ou 3 derniers (cotes courantes, pas cotes d'ouverture)
                        const odds = allNums.length >= 3 ? allNums.slice(-3) : allNums.slice(-2);
                        found.push({
                            book: book,
                            priority: PRIORITY[book],
                            odds: odds
                        });
                    }
                }
            }

            // Retourner la source la plus fiable (priorite la plus basse = meilleur)
            found.sort((a, b) => a.priority - b.priority);
            return found.length ? found[0] : null;
        ''')

        return result  # { book: "Pinnacle", odds: [...] } or None

    except Exception as e:
        print(f"[odds] Erreur match page {match_url}: {e}")
        return None


# ── Construction des events a partir des stubs ──

def _build_events_from_stubs(stubs, sport_name, market="h2h", threshold=None):
    """
    Pour chaque stub (home, away, match_url), visite la page du match
    et extrait les cotes Pinnacle pour le marche demande.
    """
    driver = _get_driver()
    if not driver:
        return []

    events = []
    for stub in stubs:
        time.sleep(1.2)  # delai poli entre les requetes
        try:
            result = _scrape_sharp_odds_from_match_page(
                driver, stub["match_url"], market=market, threshold=threshold
            )

            if not result:
                continue

            raw_odds = result["odds"]
            ref_source = result["book"].lower().replace(" ", "_") + "_via_oddsportal"

            # Sanity check : la marge totale doit etre proche de celle de Pinnacle (~2%)
            # Si elle depasse 6%, on a probablement capture un book soft par erreur
            total_implied = sum(1.0 / o for o in raw_odds if o > 1.0)
            if not (0.97 <= total_implied <= 1.06):
                print(f"[odds] Marge suspecte {total_implied:.3f} "
                      f"({stub['home']} vs {stub['away']}, {market}) — ignore")
                continue

            home = stub["home"]
            away = stub["away"]

            # Construire les outcomes selon le marche
            if market == "h2h":
                if len(raw_odds) == 3:
                    outcomes = [
                        {"name": home, "odds": raw_odds[0]},
                        {"name": "Draw", "odds": raw_odds[1]},
                        {"name": away, "odds": raw_odds[2]},
                    ]
                else:
                    outcomes = [
                        {"name": home, "odds": raw_odds[0]},
                        {"name": away, "odds": raw_odds[1]},
                    ]
                market_key = "h2h"

            elif market == "over_under":
                t = threshold or 2.5
                outcomes = [
                    {"name": f"Over {t}", "odds": raw_odds[0]},
                    {"name": f"Under {t}", "odds": raw_odds[1]},
                ]
                market_key = f"over_under_{t}"

            elif market == "btts":
                outcomes = [
                    {"name": "Oui", "odds": raw_odds[0]},
                    {"name": "Non", "odds": raw_odds[1]},
                ]
                market_key = "btts"

            else:
                continue

            valid = [o for o in outcomes if o["odds"] > 1.01]
            if len(valid) < 2:
                continue

            event = {
                "event_id": "",
                "sport_title": sport_name,
                "home_team": home,
                "away_team": away,
                "commence_time": "",
                "market": market_key,
                "market_type": market,
                "market_threshold": threshold if market == "over_under" else None,
                "num_books": 1,
                "source": ref_source,
                "outcomes": valid,
            }

            events.append(event)
            book_label = result["book"]
            t_label = f" {threshold}" if market == "over_under" else ""
            print(f"[odds] {book_label} ({market}{t_label}): {home} vs {away}")

        except Exception as e:
            print(f"[odds] Erreur stub {stub.get('home','?')} vs {stub.get('away','?')}: {e}")

    return events


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


def get_reference_odds(sport_key, markets="h2h"):
    """
    Recupere les cotes de reference Pinnacle pour un sport.
    Phase 1 : overview pages pour les URLs de matchs (avec cache)
    Phase 2 : pages individuelles pour les cotes Pinnacle specifiques
    markets : "h2h", "over_under", "btts" ou "all"
    """
    base_key = sport_key
    if base_key not in ODDSPORTAL_URLS:
        base_key = sport_key.split("_")[0]
    if base_key not in ODDSPORTAL_URLS:
        return []

    sport_name, urls = ODDSPORTAL_URLS[base_key]
    driver = _get_driver()
    if not driver:
        return []

    all_events = []

    for url in urls:
        # Phase 1 : recuperer les stubs (cache partage entre tous les marches)
        stubs = _get_match_stubs(driver, url)
        if not stubs:
            continue

        # H2H
        if markets in ("h2h", "all"):
            events = _build_events_from_stubs(stubs, sport_name, market="h2h")
            all_events.extend(events)
            print(f"[odds] {sport_name} H2H: {len(events)} events avec cotes sharp")

        # Over/Under 1.5 / 2.5 / 3.5 (football seulement)
        if markets in ("over_under", "all") and base_key == "soccer":
            for threshold in [1.5, 2.5, 3.5]:
                events = _build_events_from_stubs(
                    stubs, sport_name, market="over_under", threshold=threshold
                )
                all_events.extend(events)
                print(f"[odds] {sport_name} O/U{threshold}: {len(events)} events")

        # BTTS (football seulement)
        if markets in ("btts", "all") and base_key == "soccer":
            events = _build_events_from_stubs(stubs, sport_name, market="btts")
            all_events.extend(events)
            print(f"[odds] {sport_name} BTTS: {len(events)} events")

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

"""
EV+ Finder -- Serveur Flask principal.
100% scraping, zero API payante.
Winamax (Selenium) + Oddsportal (Selenium) = value bets.
"""

import sys
import os
import time
import threading

# Forcer stdout unbuffered (sinon les prints des threads n'apparaissent pas)
os.environ["PYTHONUNBUFFERED"] = "1"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

import csv
import io
from flask import Flask, render_template, jsonify, request, Response

from config import FLASK_HOST, FLASK_PORT, FLASK_DEBUG, MIN_EV_THRESHOLD, CACHE_DURATION
from scraper import get_all_events, get_sports, WINAMAX_SPORTS, get_match_result
from odds_api import get_reference_odds, get_available_sports, ODDSPORTAL_URLS
from ev_calculator import find_value_bets
from bankroll import place_bets, settle_bets, get_bankroll_summary, reset_bankroll, settle_bet_manually

app = Flask(__name__)

# ── Cache en memoire ──

_cache = {
    "value_bets": [],
    "winamax_events": [],
    "last_update": 0,
    "status": "idle",
    "error": None,
    "stats": {},
    "logs": [],
    "progress": 0,
}
_lock = threading.Lock()


def _log(msg):
    """Ajoute un message au log visible sur le dashboard."""
    with _lock:
        _cache["logs"].append({"time": time.time(), "msg": msg})
        # Garder les 30 derniers logs
        if len(_cache["logs"]) > 30:
            _cache["logs"] = _cache["logs"][-30:]
    print(f"[app] {msg}")


def _refresh_data():
    """Rafraichit les donnees Winamax + Oddsportal."""
    global _cache

    with _lock:
        if _cache["status"] == "loading":
            return
        _cache["status"] = "loading"
        _cache["error"] = None
        _cache["logs"] = []
        _cache["progress"] = 0

    try:
        # 1. Scraper Winamax
        _log("Demarrage Chrome headless...")
        _log("Scraping des cotes Winamax...")

        with _lock:
            _cache["progress"] = 10

        wm_events = get_all_events()
        _log(f"{len(wm_events)} evenements Winamax recuperes")

        with _lock:
            _cache["progress"] = 25

        # 2. Scraper Oddsportal (reference multi-books)
        _log("Scraping Oddsportal (cotes de reference)...")
        all_value_bets = []

        # Regrouper les events Winamax par sport
        sports_map = {}
        for ev in wm_events:
            key = ev.get("sport_api_key", "")
            if key:
                sports_map.setdefault(key, []).append(ev)

        sport_keys = list(sports_map.keys())
        total_sports = len(sport_keys)

        for idx, sport_key in enumerate(sport_keys):
            events = sports_map[sport_key]
            sport_label = sport_key
            # Trouver le nom lisible
            if sport_key in ODDSPORTAL_URLS:
                sport_label = ODDSPORTAL_URLS[sport_key][0]
            else:
                for wm in events[:1]:
                    sport_label = wm.get("sport", sport_key)

            _log(f"Comparaison {sport_label} ({len(events)} matchs Winamax)...")

            with _lock:
                _cache["progress"] = 25 + int(60 * (idx / max(total_sports, 1)))

            # Note: O/U et BTTS via match pages Oddsportal ne fonctionnent pas
            # (navigation SPA + fragment URL #over-under;2 inoperant en headless)
            # Seul H2H via Betfair/Pinnacle est fiable pour l'instant.
            markets_param = "h2h"
            ref_odds = get_reference_odds(sport_key, markets=markets_param)
            if ref_odds:
                _log(f"{sport_label}: {len(ref_odds)} matchs de reference trouves")
                vb = find_value_bets(events, ref_odds, MIN_EV_THRESHOLD)
                if vb:
                    all_value_bets.extend(vb)
                    _log(f"{sport_label}: {len(vb)} value bets detectes!")
                else:
                    _log(f"{sport_label}: aucun value bet")
            else:
                _log(f"{sport_label}: pas de donnees Oddsportal")

        with _lock:
            _cache["progress"] = 90

        # Dedupliquer et trier
        _log("Analyse et tri des resultats...")
        seen = set()
        unique_bets = []
        for vb in all_value_bets:
            key = f"{vb['home']}_{vb['away']}_{vb['bet_on']}_{vb['market']}"
            if key not in seen:
                seen.add(key)
                unique_bets.append(vb)
        unique_bets.sort(key=lambda x: x["ev_percent"], reverse=True)

        # 3. Bankroll: settlement des paris existants
        _log("Verification des paris en attente...")
        with _lock:
            _cache["progress"] = 92
        try:
            settle_result = settle_bets(get_match_result)
            if settle_result["settled"] > 0:
                _log(f"{settle_result['settled']} paris resolus!")
            if settle_result["still_pending"] > 0:
                _log(f"{settle_result['still_pending']} paris encore en attente")
        except Exception as e:
            _log(f"Erreur settlement: {e}")

        # 4. Bankroll: placement des nouveaux paris (Kelly)
        _log("Placement des nouveaux paris (Kelly)...")
        with _lock:
            _cache["progress"] = 95
        try:
            place_result = place_bets(unique_bets)
            if place_result["placed"] > 0:
                _log(f"{place_result['placed']} nouveaux paris places!")
            else:
                _log("Aucun nouveau pari a placer")
        except Exception as e:
            _log(f"Erreur placement: {e}")

        # Stats
        sports_count = {}
        for vb in unique_bets:
            s = vb.get("sport", "?")
            sports_count[s] = sports_count.get(s, 0) + 1

        avg_ev = 0
        if unique_bets:
            avg_ev = round(sum(v["ev_percent"] for v in unique_bets) / len(unique_bets), 2)

        with _lock:
            _cache["value_bets"] = unique_bets
            _cache["winamax_events"] = wm_events
            _cache["last_update"] = time.time()
            _cache["status"] = "ready"
            _cache["progress"] = 100
            _cache["stats"] = {
                "total_bets": len(unique_bets),
                "total_events": len(wm_events),
                "avg_ev": avg_ev,
                "by_sport": sports_count,
                "top_sport": max(sports_count, key=sports_count.get) if sports_count else "-",
            }

        _log(f"Termine : {len(unique_bets)} value bets trouves!")

    except Exception as e:
        _log(f"Erreur : {e}")
        import traceback
        traceback.print_exc()
        with _lock:
            _cache["status"] = "error"
            _cache["error"] = str(e)
            _cache["progress"] = 0


# ── Routes Flask ──

@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/valuebets")
def api_valuebets():
    now = time.time()
    if now - _cache["last_update"] > CACHE_DURATION and _cache["status"] != "loading":
        thread = threading.Thread(target=_refresh_data, daemon=True)
        thread.start()

    sport = request.args.get("sport", "")
    min_ev = float(request.args.get("min_ev", 0))
    min_odds = float(request.args.get("min_odds", 0))
    max_odds = float(request.args.get("max_odds", 999))

    bets = _cache["value_bets"]
    if sport:
        bets = [b for b in bets if b["sport"].lower() == sport.lower()]
    if min_ev > 0:
        bets = [b for b in bets if b["ev_percent"] >= min_ev]
    if min_odds > 0:
        bets = [b for b in bets if b["winamax_odds"] >= min_odds]
    if max_odds < 999:
        bets = [b for b in bets if b["winamax_odds"] <= max_odds]

    return jsonify({
        "bets": bets,
        "status": _cache["status"],
        "last_update": _cache["last_update"],
        "error": _cache["error"],
        "stats": _cache["stats"],
        "logs": _cache["logs"],
        "progress": _cache["progress"],
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    if _cache["status"] == "loading":
        return jsonify({"message": "Refresh deja en cours"}), 429
    thread = threading.Thread(target=_refresh_data, daemon=True)
    thread.start()
    return jsonify({"message": "Refresh lance"})


@app.route("/api/sports")
def api_sports():
    return jsonify({"sports": get_sports()})


@app.route("/api/status")
def api_status():
    return jsonify({
        "status": _cache["status"],
        "last_update": _cache["last_update"],
        "error": _cache["error"],
        "stats": _cache["stats"],
        "logs": _cache["logs"],
        "progress": _cache["progress"],
    })


# ── Routes Bankroll ──

@app.route("/api/bankroll")
def api_bankroll():
    return jsonify(get_bankroll_summary())


@app.route("/api/bankroll/settle", methods=["POST"])
def api_bankroll_settle():
    result = settle_bets(get_match_result, force=True)
    return jsonify(result)


@app.route("/api/bankroll/settle_manual", methods=["POST"])
def api_bankroll_settle_manual():
    body = request.get_json(silent=True) or {}
    bet_id = body.get("bet_id", "")
    result = body.get("result", "")
    score = body.get("score", "")
    if not bet_id or not result:
        return jsonify({"success": False, "message": "bet_id et result requis"}), 400
    return jsonify(settle_bet_manually(bet_id, result, score))


@app.route("/api/bankroll/reset", methods=["POST"])
def api_bankroll_reset():
    amount = 100.0
    if request.is_json and request.json.get("amount"):
        amount = float(request.json["amount"])
    summary = reset_bankroll(amount)
    return jsonify({"message": "Bankroll reinitialisee", "summary": summary})


@app.route("/api/bankroll/export")
def api_bankroll_export():
    data = get_bankroll_summary()
    bets = data.get("recent_bets", [])

    buf = io.StringIO()
    buf.write('\ufeff')  # BOM for Excel UTF-8
    writer = csv.writer(buf, delimiter=';')
    writer.writerow([
        "Date", "Sport", "Match", "Pari", "Marche", "Cote Winamax",
        "Prob Reelle %", "EV %", "Mise EUR", "Retour Potentiel EUR",
        "Statut", "Profit EUR", "Kelly Fraction", "Match ID"
    ])

    from datetime import datetime
    for b in bets:
        dt = datetime.fromtimestamp(b.get("placed_at", 0)).strftime("%d/%m/%Y %H:%M")
        match = f"{b.get('home', '')} vs {b.get('away', '')}"
        status_map = {"pending": "En attente", "won": "Gagne", "lost": "Perdu", "void": "Void"}
        status = status_map.get(b.get("status", ""), b.get("status", ""))
        profit = b.get("profit")
        profit_str = f"{profit:.2f}" if profit is not None else ""

        writer.writerow([
            dt, b.get("sport", ""), match, b.get("bet_on", ""),
            b.get("market", ""), f"{b.get('winamax_odds', 0):.2f}",
            f"{b.get('fair_prob', 0):.1f}", f"{b.get('ev_percent', 0):.2f}",
            f"{b.get('stake', 0):.2f}", f"{b.get('potential_return', 0):.2f}",
            status, profit_str,
            f"{b.get('kelly_fraction', 0):.6f}", b.get("match_id", "")
        ])

    # Summary row
    writer.writerow([])
    writer.writerow(["RESUME"])
    writer.writerow(["Bankroll initiale", f"{data.get('initial_bankroll', 100):.2f} EUR"])
    writer.writerow(["Bankroll actuelle", f"{data.get('current_bankroll', 0):.2f} EUR"])
    writer.writerow(["Total mise", f"{data.get('total_staked', 0):.2f} EUR"])
    writer.writerow(["Profit total", f"{data.get('total_profit', 0):.2f} EUR"])
    writer.writerow(["ROI", f"{data.get('roi', 0):.1f}%"])
    writer.writerow(["Win Rate", f"{data.get('win_rate', 0):.1f}%"])
    writer.writerow(["Paris total", data.get("total_bets", 0)])
    writer.writerow(["En attente", data.get("pending_bets", 0)])

    output = buf.getvalue()
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=ev_finder_ledger.csv"}
    )


# ── Demarrage ──

if __name__ == "__main__":
    print("=" * 50)
    print("  EV+ FINDER - 100% Scraping")
    print("  Winamax + Oddsportal (zero API)")
    print("=" * 50)

    print("\n[*] Premier chargement des donnees...")
    threading.Thread(target=_refresh_data, daemon=True).start()

    print(f"[*] Dashboard : http://localhost:{FLASK_PORT}")
    print()
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG)

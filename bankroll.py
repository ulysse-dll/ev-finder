"""
Bankroll Manager — Livre de compte virtuel.
Gestion du bankroll, sizing Kelly, placement et suivi des paris.
Persistence dans bankroll.json.
"""

import json
import os
import time
import uuid
import threading

from config import (
    BANKROLL_INITIAL, KELLY_FRACTION, MAX_STAKE_PERCENT,
    MIN_STAKE, MIN_EV_TO_BET, MIN_BOOKS_TO_BET, AUTO_BET,
)

BANKROLL_FILE = os.path.join(os.path.dirname(__file__), "bankroll.json")
_lock = threading.Lock()


# ── Persistence ──

def _load_bankroll():
    """Charge le bankroll depuis le disque, ou cree un defaut."""
    if os.path.exists(BANKROLL_FILE):
        with open(BANKROLL_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "initial_bankroll": BANKROLL_INITIAL,
        "current_bankroll": BANKROLL_INITIAL,
        "total_staked": 0.0,
        "total_returned": 0.0,
        "created_at": int(time.time()),
        "last_updated": int(time.time()),
        "bets": [],
    }


def _save_bankroll(data):
    """Sauvegarde atomique du bankroll sur disque."""
    data["last_updated"] = int(time.time())
    tmp = BANKROLL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, BANKROLL_FILE)


# ── Kelly Criterion ──

def calculate_kelly_stake(odds, fair_prob_pct, bankroll):
    """
    Calcule la mise optimale avec le critere de Kelly fractionnaire.

    Args:
        odds: Cote decimale (ex: 1.85)
        fair_prob_pct: Probabilite reelle en % (ex: 58.2)
        bankroll: Bankroll actuel en EUR

    Returns:
        dict avec kelly_full, kelly_fraction, stake, kelly_used
    """
    p = fair_prob_pct / 100.0
    q = 1.0 - p
    b = odds - 1.0

    if b <= 0 or p <= 0:
        return {"kelly_full": 0, "kelly_fraction": 0, "stake": 0, "kelly_used": KELLY_FRACTION}

    # Kelly: f = (bp - q) / b
    kelly_full = (b * p - q) / b

    if kelly_full <= 0:
        return {"kelly_full": 0, "kelly_fraction": 0, "stake": 0, "kelly_used": KELLY_FRACTION}

    # Quarter Kelly + cap
    kelly_frac = min(kelly_full * KELLY_FRACTION, MAX_STAKE_PERCENT)
    stake = round(bankroll * kelly_frac, 2)

    if stake < MIN_STAKE:
        stake = 0

    return {
        "kelly_full": round(kelly_full, 6),
        "kelly_fraction": round(kelly_frac, 6),
        "stake": stake,
        "kelly_used": KELLY_FRACTION,
    }


# ── Placement des paris ──

def place_bets(value_bets):
    """
    Evalue les value bets et place automatiquement ceux qui passent les filtres.
    Deduplication par match_id + bet_on + market.

    Returns:
        dict avec placed, skipped, details
    """
    if not AUTO_BET:
        return {"placed": 0, "skipped": len(value_bets), "details": []}

    with _lock:
        data = _load_bankroll()

        # Cles existantes pour dedup
        existing_keys = set()
        for b in data["bets"]:
            key = f"{b.get('match_id', '')}_{b['bet_on']}_{b['market']}"
            existing_keys.add(key)

        placed = []
        skipped = 0

        for vb in value_bets:
            match_id = vb.get("match_id", "")
            if not match_id:
                skipped += 1
                continue

            # Dedup
            dedup_key = f"{match_id}_{vb['bet_on']}_{vb['market']}"
            if dedup_key in existing_keys:
                skipped += 1
                continue

            # Filtres qualite
            if vb["ev_percent"] < MIN_EV_TO_BET:
                skipped += 1
                continue
            if vb.get("num_books", 0) < MIN_BOOKS_TO_BET:
                skipped += 1
                continue

            # Calcul mise Kelly
            sizing = calculate_kelly_stake(
                vb["winamax_odds"],
                vb["fair_prob"],
                data["current_bankroll"],
            )

            if sizing["stake"] <= 0:
                skipped += 1
                continue

            bet_record = {
                "bet_id": uuid.uuid4().hex[:12],
                "placed_at": int(time.time()),
                "sport": vb["sport"],
                "home": vb["home"],
                "away": vb["away"],
                "market": vb["market"],
                "market_type": vb.get("market_type", "h2h"),
                "market_threshold": vb.get("market_threshold"),
                "bet_on": vb["bet_on"],
                "winamax_odds": vb["winamax_odds"],
                "fair_prob": vb["fair_prob"],
                "implied_prob": vb.get("implied_prob", 0),
                "ev_percent": vb["ev_percent"],
                "match_id": match_id,
                "start_time": vb.get("start_time", 0),
                "stake": sizing["stake"],
                "kelly_fraction": sizing["kelly_fraction"],
                "kelly_used": sizing["kelly_used"],
                "potential_return": round(sizing["stake"] * vb["winamax_odds"], 2),
                "status": "pending",
                "settled_at": None,
                "profit": None,
                "result_info": None,
            }

            data["bets"].append(bet_record)
            data["current_bankroll"] = round(data["current_bankroll"] - sizing["stake"], 2)
            data["total_staked"] = round(data["total_staked"] + sizing["stake"], 2)
            existing_keys.add(dedup_key)
            placed.append(bet_record)

        _save_bankroll(data)

    return {"placed": len(placed), "skipped": skipped, "details": placed}


# ── Settlement des paris ──

def settle_bets(get_result_fn, force=False):
    """
    Verifie les paris en attente et les resout (won/lost/void).

    Args:
        get_result_fn: callable(match_id) -> dict ou None
        force: si True, renvoie le detail par pari (pour le bouton CHECK RESULTS)

    Returns:
        dict avec settled, still_pending, details, bet_reports
    """
    with _lock:
        data = _load_bankroll()
        settled = []
        still_pending = 0
        now = int(time.time())
        bet_reports = []

        for bet in data["bets"]:
            if bet["status"] != "pending":
                continue

            match_label = f"{bet['home']} vs {bet['away']}"
            start = bet.get("start_time", 0)

            # Match pas encore commence
            if start > 0 and start > now:
                still_pending += 1
                if force:
                    remaining = start - now
                    hours = remaining // 3600
                    mins = (remaining % 3600) // 60
                    bet_reports.append({
                        "bet_id": bet["bet_id"],
                        "match": match_label,
                        "bet_on": bet["bet_on"],
                        "reason": "not_started",
                        "message": f"Coup d'envoi dans {hours}h{mins:02d}",
                    })
                continue

            # Match commence mais < 2h (probablement en cours)
            if start > 0 and start > now - 7200:
                still_pending += 1
                if force:
                    elapsed = (now - start) // 60
                    bet_reports.append({
                        "bet_id": bet["bet_id"],
                        "match": match_label,
                        "bet_on": bet["bet_on"],
                        "reason": "in_progress",
                        "message": f"Match en cours ({elapsed} min)",
                    })
                continue

            # Essayer de recuperer le resultat
            try:
                result = get_result_fn(
                    bet["match_id"],
                    home=bet.get("home", ""),
                    away=bet.get("away", ""),
                    start_time=bet.get("start_time", 0),
                    sport=bet.get("sport", "Football"),
                )
            except Exception as e:
                print(f"[bankroll] Erreur result check {bet['match_id']}: {e}")
                still_pending += 1
                if force:
                    bet_reports.append({
                        "bet_id": bet["bet_id"],
                        "match": match_label,
                        "bet_on": bet["bet_on"],
                        "reason": "error",
                        "message": f"Erreur: {str(e)[:60]}",
                    })
                continue

            if result is None:
                still_pending += 1
                if force:
                    bet_reports.append({
                        "bet_id": bet["bet_id"],
                        "match": match_label,
                        "bet_on": bet["bet_on"],
                        "reason": "no_result",
                        "message": "Resultat pas encore disponible",
                    })
                continue

            if result.get("status") == "cancelled":
                bet["status"] = "void"
                bet["settled_at"] = now
                bet["profit"] = 0.0
                bet["result_info"] = "Annule"
                data["current_bankroll"] = round(data["current_bankroll"] + bet["stake"], 2)
                settled.append(bet)
                if force:
                    bet_reports.append({
                        "bet_id": bet["bet_id"],
                        "match": match_label,
                        "bet_on": bet["bet_on"],
                        "reason": "void",
                        "message": "Match annule — mise remboursee",
                    })
                continue

            # Determiner victoire/defaite
            market_type = bet.get("market_type", "h2h")
            threshold = bet.get("market_threshold")
            won = _check_win_extended(bet["bet_on"], result, market_type, threshold)
            score = result.get("score", "")

            # Si le score n'est pas encore lisible, laisser en attente
            if won is None:
                still_pending += 1
                if force:
                    bet_reports.append({
                        "bet_id": bet["bet_id"],
                        "match": match_label,
                        "bet_on": bet["bet_on"],
                        "reason": "no_result",
                        "message": "Score non disponible pour resoudre ce marche",
                    })
                continue

            if won:
                bet["status"] = "won"
                payout = round(bet["stake"] * bet["winamax_odds"], 2)
                bet["profit"] = round(payout - bet["stake"], 2)
                data["current_bankroll"] = round(data["current_bankroll"] + payout, 2)
                data["total_returned"] = round(data["total_returned"] + payout, 2)
                if force:
                    bet_reports.append({
                        "bet_id": bet["bet_id"],
                        "match": match_label,
                        "bet_on": bet["bet_on"],
                        "reason": "won",
                        "message": f"GAGNE ! Score: {score} — +{bet['profit']:.2f} EUR",
                    })
            else:
                bet["status"] = "lost"
                bet["profit"] = round(-bet["stake"], 2)
                if force:
                    bet_reports.append({
                        "bet_id": bet["bet_id"],
                        "match": match_label,
                        "bet_on": bet["bet_on"],
                        "reason": "lost",
                        "message": f"PERDU. Score: {score} — {bet['profit']:.2f} EUR",
                    })

            bet["settled_at"] = now
            bet["result_info"] = score
            settled.append(bet)

        _save_bankroll(data)

    return {
        "settled": len(settled),
        "still_pending": still_pending,
        "details": settled,
        "bet_reports": bet_reports,
    }


def _check_win(bet_on, winning_outcomes):
    """Verifie si bet_on fait partie des outcomes gagnants (fuzzy)."""
    from ev_calculator import _normalize_name

    bet_norm = _normalize_name(bet_on)
    draw_names = {"match nul", "nul", "draw", "x", "tie"}

    for w in winning_outcomes:
        if _normalize_name(w) == bet_norm:
            return True

    # Match nul
    if bet_norm in draw_names:
        for w in winning_outcomes:
            if _normalize_name(w) in draw_names:
                return True

    return False


def _parse_score(score_str):
    """Parse '2-1' ou '2:1' -> (2, 1). Retourne (-1, -1) si invalide."""
    if not score_str:
        return -1, -1
    for sep in ["-", ":", " - ", " "]:
        if sep in score_str:
            parts = score_str.split(sep)
            parts = [p.strip() for p in parts if p.strip().isdigit()]
            if len(parts) >= 2:
                return int(parts[0]), int(parts[1])
    return -1, -1


def _check_win_extended(bet_on, result, market_type="h2h", threshold=None):
    """
    Determine si un pari est gagnant selon le market_type.
    Retourne True/False, ou None si le resultat ne peut pas etre determine.
    """
    if market_type == "h2h":
        return _check_win(bet_on, result.get("winning_outcomes", []))

    score = result.get("score", "")
    hs, as_ = _parse_score(score)

    if hs < 0 or as_ < 0:
        return None  # Score invalide, ne peut pas determiner

    if market_type == "over_under":
        total = hs + as_
        label = bet_on.lower()
        if any(k in label for k in ["plus", "over"]):
            return total > threshold
        else:
            return total <= threshold

    if market_type == "btts":
        btts = hs > 0 and as_ > 0
        label = bet_on.lower()
        return btts if any(k in label for k in ["oui", "yes"]) else not btts

    return None


# ── Statistiques ──

def get_bankroll_summary():
    """Retourne l'etat complet du bankroll pour le dashboard."""
    with _lock:
        data = _load_bankroll()

    bets = data["bets"]

    pending = [b for b in bets if b["status"] == "pending"]
    won = [b for b in bets if b["status"] == "won"]
    lost = [b for b in bets if b["status"] == "lost"]
    settled = won + lost

    total_profit = sum(b["profit"] for b in settled if b["profit"] is not None)
    total_settled_stakes = sum(b["stake"] for b in settled)
    win_rate = (len(won) / len(settled) * 100) if settled else 0
    roi = (total_profit / total_settled_stakes * 100) if total_settled_stakes > 0 else 0

    # Historique P/L pour le graphique
    pl_history = []
    cumulative = 0
    for b in sorted(settled, key=lambda x: x.get("settled_at", 0)):
        cumulative += b["profit"] or 0
        pl_history.append({
            "timestamp": b.get("settled_at", 0),
            "cumulative_pl": round(cumulative, 2),
            "bankroll": round(data["initial_bankroll"] + cumulative, 2),
            "bet_id": b["bet_id"],
        })

    # Paris recents (50 max, plus recents d'abord)
    recent = sorted(bets, key=lambda x: x.get("placed_at", 0), reverse=True)[:50]

    return {
        "initial_bankroll": data["initial_bankroll"],
        "current_bankroll": data["current_bankroll"],
        "total_staked": data["total_staked"],
        "total_returned": round(data["total_returned"], 2),
        "total_profit": round(total_profit, 2),
        "total_bets": len(bets),
        "pending_bets": len(pending),
        "won_bets": len(won),
        "lost_bets": len(lost),
        "win_rate": round(win_rate, 1),
        "roi": round(roi, 1),
        "pl_history": pl_history,
        "recent_bets": recent,
        "created_at": data.get("created_at", 0),
    }


# ── Règlement manuel ──

def settle_bet_manually(bet_id, result, score=""):
    """
    Règle manuellement un pari (won / lost / void).

    Args:
        bet_id: identifiant du pari
        result: "won", "lost" ou "void"
        score:  score optionnel ex "1-0"

    Returns:
        dict avec success, message, summary
    """
    if result not in ("won", "lost", "void"):
        return {"success": False, "message": f"Résultat invalide: {result}"}

    with _lock:
        data = _load_bankroll()
        bet = next((b for b in data["bets"] if b["bet_id"] == bet_id), None)

        if not bet:
            return {"success": False, "message": f"Pari {bet_id} introuvable"}
        if bet["status"] != "pending":
            return {"success": False, "message": f"Pari déjà réglé ({bet['status']})"}

        now = int(time.time())
        bet["settled_at"] = now
        bet["result_info"] = score or "manuel"

        if result == "won":
            payout = round(bet["stake"] * bet["winamax_odds"], 2)
            bet["profit"] = round(payout - bet["stake"], 2)
            bet["status"] = "won"
            data["current_bankroll"] = round(data["current_bankroll"] + payout, 2)
            data["total_returned"] = round(data["total_returned"] + payout, 2)
            msg = f"GAGNE — +{bet['profit']:.2f} EUR"
        elif result == "lost":
            bet["profit"] = round(-bet["stake"], 2)
            bet["status"] = "lost"
            msg = f"PERDU — {bet['profit']:.2f} EUR"
        else:  # void
            bet["profit"] = 0.0
            bet["status"] = "void"
            data["current_bankroll"] = round(data["current_bankroll"] + bet["stake"], 2)
            msg = "VOID — mise remboursée"

        _save_bankroll(data)

    return {"success": True, "message": msg, "summary": get_bankroll_summary()}


# ── Reset ──

def reset_bankroll(amount=None):
    """Reinitialise le bankroll."""
    if amount is None:
        amount = BANKROLL_INITIAL
    with _lock:
        data = {
            "initial_bankroll": amount,
            "current_bankroll": amount,
            "total_staked": 0.0,
            "total_returned": 0.0,
            "created_at": int(time.time()),
            "last_updated": int(time.time()),
            "bets": [],
        }
        _save_bankroll(data)
    return get_bankroll_summary()

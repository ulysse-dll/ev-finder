# ── EV+ Finder — Configuration ──

# Seuil minimum EV pour afficher un pari (en %)
MIN_EV_THRESHOLD = 0.0

# Cache duree en secondes
CACHE_DURATION = 120

# Flask
FLASK_HOST = "0.0.0.0"
FLASK_PORT = 5120
FLASK_DEBUG = False

# The Odds API (optionnel, pas necessaire)
ODDS_API_KEY = ""
ODDS_API_BASE = ""
ODDS_API_REGIONS = ""

# Bankroll Management
BANKROLL_INITIAL = 100.0        # Bankroll de depart en EUR
KELLY_FRACTION = 0.25           # Quarter Kelly (conservateur)
MAX_STAKE_PERCENT = 0.05        # Max 5% du bankroll par pari
MIN_STAKE = 0.10                # Mise minimum en EUR
MIN_EV_TO_BET = 1.0             # EV% minimum pour placer un pari
MIN_BOOKS_TO_BET = 3            # Minimum de bookmakers dans le consensus
AUTO_BET = True                 # Placement automatique des paris

"""Settings and secrets. Secrets come from .env (never commit .env)."""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass

HERE = Path(__file__).resolve().parent

SM8_API_KEY    = os.getenv("SM8_API_KEY", "")
BREVO_API_KEY  = os.getenv("BREVO_API_KEY", "")
BREVO_MASTER_LIST_ID = int(os.getenv("BREVO_MASTER_LIST_ID", "18"))
GSHEET_CREDS   = os.getenv("GSHEET_CREDS", str(HERE / "gcp-service-account.json"))
SHEET_ID       = os.getenv("SHEET_ID", "1ZYlZqqL2fNN1--t282adwLe1vnUfaEUIOjsaQDViWjU")

# Safe by default: nothing is written anywhere until DRY_RUN=false in .env
DRY_RUN = os.getenv("DRY_RUN", "true").strip().lower() not in ("false", "0", "no")

LOG_DIR = HERE / "logs"

# Retention rules
DUE_MONTHS            = 12    # every customer is due 12 months after their last service
SUPPRESS_DAYS         = 180   # "Not interested" -> 6 months off the lists
BOOKED_SUPPRESS_DAYS  = 90    # admin "Booked" -> 90 days off the lists
NO_ANSWER_LIMIT       = 5     # 5 x "No answer" in a bucket -> 6-month suppression
COLD_MIN_MONTHS       = 3     # never-converted leads must be quiet this long for Brevo

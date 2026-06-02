from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
DB_PATH = OUTPUT_DIR / "c_analysis.db"
MIDDLEWARE_RULES_PATH = BASE_DIR / "middleware_rules.json"
MIDDLEWARE_RULES_TEMPLATE_PATH = BASE_DIR / "middleware_rules.template.json"

SOURCE_EXTENSIONS = {".c", ".h"}
EXCLUDE_DIRS = {
    ".git",
    "build",
    "out",
    "Debug",
    "Release",
    ".vs",
}

COMMIT_INTERVAL = 100

"""Settings. Everything is overridable by environment variable so the service
can be pointed at a scratch knowledge base during tests."""

import os
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, ""))
    except ValueError:
        return default


class Settings:
    def __init__(self) -> None:
        # The OKF bundle. Plain markdown, safe to keep in git.
        self.root: Path = _env_path("JOBKB_ROOT", Path.home() / ".jobkb")
        # Derived index. Disposable: delete it and it rebuilds from markdown.
        self.index_dir: Path = self.root / ".index"

        self.api_key: str = os.environ.get("OPENROUTER_API_KEY", "").strip()
        self.host: str = os.environ.get("JOBKB_HOST", "127.0.0.1")
        self.port: int = _env_int("JOBKB_PORT", 8765)
        # Optional shared secret. When set, every request must carry
        # `X-JobKB-Token: <value>`.
        self.token: str = os.environ.get("JOBKB_TOKEN", "").strip()

        self.request_timeout: int = _env_int("JOBKB_TIMEOUT", 90)
        self.model_refresh_seconds: int = _env_int("JOBKB_MODEL_REFRESH", 3600)
        # How many models deep the fallback ladder goes.
        self.ladder_depth: int = _env_int("JOBKB_LADDER_DEPTH", 5)
        # Candidates handed to the routing model per form.
        self.retrieve_top_k: int = _env_int("JOBKB_TOP_K", 40)

        # Where your resume lives, so the service picks it up itself.
        #
        # JOBKB_RESUME       the plain-text version (.txt/.md), which grounds
        #                    drafted answers
        # JOBKB_RESUME_FILE  the original document (.pdf/.docx), which gets
        #                    attached to upload fields
        #
        # Either may be given alone. Both are re-read at every start and only
        # rewritten when the file on disk has actually changed, so pointing at a
        # resume you keep editing just works.
        #
        # Under Podman these are paths *inside the container*: bind mount the
        # folder first, or use the picker in the extension's Options instead.
        raw_resume = os.environ.get("JOBKB_RESUME", "").strip()
        raw_resume_file = os.environ.get("JOBKB_RESUME_FILE", "").strip()
        self.resume_path: Path | None = Path(raw_resume).expanduser() if raw_resume else None
        self.resume_file_path: Path | None = (
            Path(raw_resume_file).expanduser() if raw_resume_file else None
        )

        # A model id to pin. Empty means "discover models at boot".
        self.pinned_model: str = os.environ.get("JOBKB_MODEL", "").strip()

        # Most a paid model may cost, in USD per million prompt tokens. 0 keeps
        # the ladder free-only.
        #
        # Worth setting on a funded account. OpenRouter's free tier is capped at
        # 20 requests per minute across every free model *regardless of credit*,
        # and that ceiling is what stalls a fill mid-form. A paid rung below the
        # free ones costs a fraction of a cent per form and is never queued.
        self.max_price: float = _env_float("JOBKB_MAX_PRICE", 0.0)

        self.referer: str = os.environ.get(
            "JOBKB_REFERER", "http://localhost/job-knowledge-service"
        )
        self.app_title: str = "Job Knowledge Service"

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)


settings = Settings()

"""Stage integration: config loading, credential management, and CLI helpers.

Precedence for resolving stage config (highest to lowest):
  1. Explicit Stage(...) passed to World()
  2. ENSEMBLE_STAGE_API_KEY + ENSEMBLE_STAGE_PROJECT environment variables
  3. ~/.ensemble/stage.toml (credentials) + ./.stage.toml (project)
  4. Nothing (Stage disabled)

ENSEMBLE_STAGE_ENABLED=0 disables Stage even when fully configured.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


PROD_BASE_URL = "https://stage.ensemble.sh"
_CREDS_PATH = Path.home() / ".ensemble" / "stage.toml"
_PROJECT_TOML = Path(".stage.toml")


@dataclass
class StageConfig:
    """Resolved stage configuration ready to pass to Rust."""
    api_key: str
    project: str
    base_url: str = PROD_BASE_URL

    @property
    def org_slug(self) -> str:
        parts = self.project.split("/", 1)
        return parts[0] if len(parts) == 2 else ""

    @property
    def project_slug(self) -> str:
        parts = self.project.split("/", 1)
        return parts[1] if len(parts) == 2 else self.project


class Stage:
    """Handle to the Stage observability backend.

    Construct with an explicit api_key and project, or let
    Stage.resolve() pick up credentials from the environment and
    config files.

    Usage:
        stage = Stage(project="myorg/popcornbench", api_key="stage_sk_...")
        world = World("plank", stage=stage)
    """

    def __init__(
        self,
        *,
        project: str,
        api_key: str,
        base_url: str = PROD_BASE_URL,
    ) -> None:
        if "/" not in project:
            raise ValueError(
                f"project must be 'org_slug/project_slug', got {project!r}"
            )
        self._project = project
        self._api_key = api_key
        self._base_url = base_url

    @property
    def project(self) -> str:
        return self._project

    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def base_url(self) -> str:
        return self._base_url

    def to_config(self) -> StageConfig:
        return StageConfig(
            api_key=self._api_key,
            project=self._project,
            base_url=self._base_url,
        )

    @staticmethod
    def resolve(explicit: Optional["Stage"] = None) -> Optional[StageConfig]:
        """Return the effective StageConfig or None if Stage is disabled.

        Resolution order:
          1. explicit Stage object (caller-supplied)
          2. ENSEMBLE_STAGE_API_KEY + ENSEMBLE_STAGE_PROJECT env vars
          3. ~/.ensemble/stage.toml + ./.stage.toml
        """
        if os.environ.get("ENSEMBLE_STAGE_ENABLED", "").strip() in ("0", "false", "no"):
            return None

        if explicit is not None:
            return explicit.to_config()

        env_key = os.environ.get("ENSEMBLE_STAGE_API_KEY", "").strip()
        env_project = os.environ.get("ENSEMBLE_STAGE_PROJECT", "").strip()
        env_base_url = os.environ.get("ENSEMBLE_STAGE_BASE_URL", "").strip()
        if env_key and env_project:
            return StageConfig(
                api_key=env_key,
                project=env_project,
                base_url=env_base_url or PROD_BASE_URL,
            )

        return _load_from_toml()

    def __repr__(self) -> str:
        return f"Stage(project={self._project!r}, base_url={self._base_url!r})"


def _load_toml_file(path: Path) -> dict:
    """Load a TOML file, returning an empty dict on any error."""
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            try:
                import toml as _toml  # type: ignore[no-redef]
                return _toml.loads(path.read_text()) if path.exists() else {}
            except ImportError:
                return {}
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except Exception:
        return {}


def _load_from_toml() -> Optional[StageConfig]:
    """Read credentials from ~/.ensemble/stage.toml and project from ./.stage.toml."""
    creds = _load_toml_file(_CREDS_PATH)
    project_cfg = _load_toml_file(_PROJECT_TOML)

    api_key = creds.get("credentials", {}).get("api_key", "").strip()
    creds_base_url = creds.get("credentials", {}).get("base_url", "").strip()
    project = project_cfg.get("stage", {}).get("project", "").strip()
    proj_base_url = project_cfg.get("stage", {}).get("base_url", "").strip()

    if not api_key or not project:
        return None

    base_url = creds_base_url or proj_base_url or PROD_BASE_URL
    return StageConfig(api_key=api_key, project=project, base_url=base_url)


def write_credentials(api_key: str, base_url: str = PROD_BASE_URL, user_login: str = "") -> None:
    """Write credentials to ~/.ensemble/stage.toml with 0600 permissions."""
    _CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = ["[credentials]\n", f'api_key = "{api_key}"\n']
    if base_url and base_url != PROD_BASE_URL:
        lines.append(f'base_url = "{base_url}"\n')
    if user_login:
        lines.append(f'user_login = "{user_login}"\n')
    _CREDS_PATH.write_text("".join(lines))
    _CREDS_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)


def write_project_toml(project: str, base_url: str = "") -> None:
    """Write project slug to ./.stage.toml in the current directory."""
    lines = ["[stage]\n", f'project = "{project}"\n']
    if base_url and base_url != PROD_BASE_URL:
        lines.append(f'base_url = "{base_url}"\n')
    _PROJECT_TOML.write_text("".join(lines))

import os

import yaml

# Template / unset markers that must never be forced into GOOGLE_APPLICATION_CREDENTIALS.
_PLACEHOLDER_CRED_PATHS = frozenset(
    {
        "",
        "<path to service account json>",
        "<path>",
        "None",
        "null",
    }
)


def _apply_google_application_credentials(config):
    """Set GOOGLE_APPLICATION_CREDENTIALS only when a real key file exists.

    On Cloud Run, omit the key and use the runtime service account (ADC).
    Placeholder values from agent_settings.yaml templates must not be applied —
    they crash worker boot with DefaultCredentialsError.
    """
    gcp = config.get("gcp") or {}
    raw = gcp.get("google_application_credentials")
    if raw is None:
        return
    path = str(raw).strip()
    if path in _PLACEHOLDER_CRED_PATHS or path.startswith("<"):
        return
    if not os.path.isfile(path):
        print(
            "WARNING: gcp.google_application_credentials is not an existing file "
            f"({path!r}); using Application Default Credentials instead."
        )
        return
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path


def get_config(argv=None):
    yaml_file = os.getenv("FUBON_AGENT_CONF")
    print("Get yaml_file path from environment variable FUBON_AGENT_CONF => %s" % yaml_file)
    if not yaml_file:
        if argv and len(argv) > 0:
            yaml_file = argv[0]
    if not yaml_file:
        print("No yaml file path provided, use settings.yaml by default.")
        yaml_file = "settings.yaml"
    with open(yaml_file, "r") as f:
        config = yaml.safe_load(f) or {}

    _apply_google_application_credentials(config)
    return config

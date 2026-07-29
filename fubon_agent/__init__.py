import os

import yaml


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

    if "gcp" in config and config["gcp"].get("google_application_credentials"):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = config["gcp"]["google_application_credentials"]
    return config

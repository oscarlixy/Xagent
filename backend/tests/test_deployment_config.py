import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_backend_services_share_one_built_image() -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose CLI is not installed")

    compose_version = subprocess.run(
        [docker, "compose", "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if compose_version.returncode != 0:
        pytest.skip("Docker Compose CLI is not available")

    project_root = Path(__file__).resolve().parents[2]
    rendered = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(project_root / ".env.example"),
            "-f",
            str(project_root / "docker-compose.yml"),
            "config",
            "--format",
            "json",
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=True,
    )
    services = json.loads(rendered.stdout)["services"]
    backend_service_names = ("backend", "migrate", "scheduler")

    build_owners = {
        service_name
        for service_name in backend_service_names
        if services[service_name].get("build") is not None
    }
    images = {
        service_name: services[service_name]["image"]
        for service_name in backend_service_names
    }

    assert build_owners == {"backend"}
    assert images == {
        "backend": "x-digest",
        "migrate": "x-digest",
        "scheduler": "x-digest",
    }
    expected_oauth_environment = {
        "X_CLIENT_ID": "replace-with-your-x-oauth-client-id",
        "X_OAUTH_REDIRECT_URI": "http://localhost:3000/api/x/callback",
    }
    assert {
        name: {
            key: services[name]["environment"][key]
            for key in expected_oauth_environment
        }
        for name in ("backend", "scheduler")
    } == {
        "backend": expected_oauth_environment,
        "scheduler": expected_oauth_environment,
    }

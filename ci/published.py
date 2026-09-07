"""Is this package's current version already in the project's registry?

Exits 0 when it is, so the publish job can skip; 1 when it is not, so the
job publishes. Every push to master runs the publish job, and the registry
refuses a duplicate file, so an unchanged version has to be recognised
rather than uploaded.

`uv publish --check-url` would do this, but it reads the PyPI simple index,
which does not accept `CI_JOB_TOKEN`. The packages API does.
"""

import json
import os
import pathlib
import sys
import tomllib
import urllib.request


def normalize(name):
    """PEP 503 name comparison, enough for our names: `_` and case differ
    between `[project].name` and what the registry reports."""
    return name.replace("_", "-").lower()


def is_published(packages, name, version):
    return any(
        normalize(package["name"]) == normalize(name)
        and package["version"] == version
        for package in packages
    )


def published_packages(api_url, project_id, job_token):
    url = f"{api_url}/projects/{project_id}/packages?package_type=pypi"
    request = urllib.request.Request(url, headers={"JOB-TOKEN": job_token})
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def main():
    project = tomllib.loads(pathlib.Path("pyproject.toml").read_text())
    name = project["project"]["name"]
    version = project["project"]["version"]
    packages = published_packages(
        os.environ["CI_API_V4_URL"],
        os.environ["CI_PROJECT_ID"],
        os.environ["CI_JOB_TOKEN"],
    )
    if is_published(packages, name, version):
        print(f"{name} {version} is already published -- nothing to do.")
        return 0
    print(f"{name} {version} is not published yet.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""The page can load its images from a public store (WEB_IMAGES_BASE_URL, e.g. DigitalOcean Spaces)."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from viharbot import web_app

BASE = "https://viharbot-assets.nyc3.cdn.digitaloceanspaces.com/images"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("WEB_AUTH_ENABLED", "false")
    return TestClient(web_app.app)


def test_without_a_base_url_the_page_uses_the_local_images_route(client, monkeypatch):
    monkeypatch.delenv("WEB_IMAGES_BASE_URL", raising=False)
    page = client.get("/").text
    assert 'src="/images/Nova_headshot.png"' in page and "digitaloceanspaces" not in page


def test_with_a_base_url_every_image_reference_points_at_the_store(client, monkeypatch):
    monkeypatch.setenv("WEB_IMAGES_BASE_URL", BASE + "/")  # a trailing slash is tolerated
    page = client.get("/").text
    assert 'src="/images/' not in page and '"/images/' not in page  # markup and script both rewritten
    assert f'src="{BASE}/Nova_headshot.png"' in page
    assert f'{BASE}/Kepler%2C%20Director%20of%20Analytics%20%26%20Foresight.png' in page
    assert f'nova: "{BASE}/Nova_headshot.png"' in page or f'"{BASE}/Nova_headshot.png"' in page
    assert f'{BASE}/AI%20Robot%20Team%20Portrait%20Grid.png' in page
    assert not re.search(r"(?<!https:)//", page.split("<body", 1)[1].split("<script", 1)[0].replace(BASE, ""))


@pytest.mark.parametrize("bad", ["http://example.com/images", "https://user:pw@example.com/images",
                                 "https://example.com/images?x=1", "https://example.com/i#f",
                                 "//example.com/images", "javascript:alert(1)", "https://exa mple.com"])
def test_an_unsafe_base_url_is_refused(monkeypatch, bad):
    monkeypatch.setenv("WEB_IMAGES_BASE_URL", bad)
    with pytest.raises(RuntimeError):
        web_app.images_base_url()


def test_the_production_image_no_longer_bakes_in_images():
    from pathlib import Path
    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile.web").read_text()
    assert "COPY images" not in dockerfile

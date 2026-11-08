# Image storage (DigitalOcean Spaces)

Status: **code ready; no Space has been created and nothing is uploaded or deployed.** The website images (portraits, logos, the historical grid) are no longer in git. The page loads them from a public base URL.

## How it works

- `WEB_IMAGES_BASE_URL` (a plain `https://` URL, for example the Space's CDN endpoint plus `/images`) makes the page load every image from that URL. The app refuses http, credentials, a query or a fragment.
- Unset, the page uses the app's own `/images/{name}` route, which serves the allowlisted names (`TEAM_IMAGES` in `web_app.py`) from `WEB_IMAGES_DIR` if that directory exists. This is for local development only; the production image no longer contains `images/`.
- **Objects in the Space are public and bypass the web app's Basic Auth.** Upload only images meant to be public. The allowlist is the list of publishable files, and the upload script uploads nothing else.

## Setup (one-off, by the founder)

1. In DigitalOcean, create a Space (choose region) and enable its CDN. Note the bucket name, region and CDN endpoint.
2. Create a Spaces access key (control panel, Spaces Object Storage, Access Keys). It is separate from the API token. Keep it out of the repository.
3. Dry-run the upload from a checkout that still has the images: `SPACES_BUCKET=... SPACES_REGION=... (plus SPACES_ACCESS_KEY and SPACES_SECRET_KEY in your shell) scripts/upload_images_to_spaces.sh /path/to/images`. Check the list, then re-run with `APPLY=yes`.
4. Set `WEB_IMAGES_BASE_URL` on the web service (for example `https://<bucket>.<region>.cdn.digitaloceanspaces.com/images`). On DigitalOcean App Platform that is an environment variable on the web component, applied only after the usual Stage promotion approval.

Cost: a Space has a flat monthly base price that includes storage and transfer allowances. Check current pricing before creating one.

## Not covered

No lifecycle rules, versioned filenames or cache purge. To change an image, upload it under the same name and wait out the one-day cache, or purge the CDN in the control panel.

# story-card-publisher

Publishes an Instagram story card for @markstulzer each morning, for whatever
post Typefully is scheduled to send that day.

Runs on GitHub Actions so it does not depend on a laptop being awake.

- `publish.py` — asks Typefully what goes out today, renders the card, publishes to Instagram
- `render_story.py` — the card renderer (Pillow)
- `cards/` — rendered cards, served publicly via jsDelivr (Instagram fetches by URL)
- `state/published.json` — draft ids already posted, so a rerun cannot double-post

**This repo is public on purpose.** Instagram will only accept an image it can
fetch over the open internet, and serving from a public repo via jsDelivr avoids
needing a separate host or a cross-repo access token. Nothing sensitive lives
here: every credential is a GitHub Actions secret, never a file.

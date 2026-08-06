#!/usr/bin/env python3
"""
Publish an Instagram story card for whatever is scheduled to post today.

Runs on GitHub Actions, so Mark's Mac can be asleep, off, or gone.

SOURCE OF TRUTH IS TYPEFULLY, NOT THE VAULT.
The vault file lives on one laptop and cannot be read from CI. Typefully already
knows what is scheduled and what text will actually go out, so this asks it
directly. That also means the story and the post can never show different words.

IDEMPOTENCY: published draft ids are appended to state/published.json and
committed back by the workflow. A rerun on the same day is a no-op. This matters
more here than locally, because Actions can retry a job.

TOKEN: passed in as a secret. This job deliberately does NOT refresh it, because
Actions cannot update its own secrets without a PAT, so a refresh here would
produce a new token with nowhere to live and the stored one would drift toward
expiry anyway. The Mac rolls the token forward instead, whenever it happens to be
on, via ig-token-sync.sh. Sixty days of the Mac never running would break it, and
that is loud rather than silent because publishing starts failing into Slack.
"""
import json, os, re, subprocess, sys, time, urllib.parse, urllib.request, urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Winnipeg")
TF_API = "https://api.typefully.com/v2/social-sets/324966"
IG_API = "https://graph.instagram.com/v21.0"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state", "published.json")
CARDS = os.path.join(HERE, "cards")

TF_KEY = os.environ.get("TYPEFULLY_API_KEY", "")
IG_USER = os.environ.get("IG_USER_ID", "")
IG_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
SLACK = os.environ.get("SLACK_WEBHOOK", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "mfstulzer/story-card-publisher")
DRY = "--dry-run" in sys.argv


def slack(msg):
    if not SLACK:
        return
    try:
        urllib.request.urlopen(urllib.request.Request(
            SLACK, data=json.dumps({"text": msg}).encode(),
            headers={"Content-Type": "application/json"}, method="POST"), timeout=20)
    except Exception:
        pass


def tf(path):
    req = urllib.request.Request(TF_API + path, headers={"Authorization": "Bearer " + TF_KEY})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def ig_post(path, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(IG_API + path, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def ig_get(path):
    with urllib.request.urlopen(IG_API + path, timeout=60) as r:
        return json.load(r)


def todays_drafts(today):
    """Every scheduled draft landing today. limit=50 is the API max; asking for
    more silently returns an empty list rather than erroring."""
    rows, offset = [], 0
    while True:
        d = tf("/drafts?limit=50&offset=%d" % offset)
        batch = d.get("results", [])
        rows += batch
        if not batch or not d.get("next"):
            break
        offset += 50
    out = []
    for r in rows:
        sd = r.get("scheduled_date")
        if not sd:
            continue
        # scheduled_date is UTC; compare in Mark's timezone, not the runner's
        local = datetime.fromisoformat(sd.replace("Z", "+00:00")).astimezone(TZ).date()
        if local.isoformat() == today:
            out.append(r)
    return out


def draft_text(draft_id):
    d = tf("/drafts/%s" % draft_id)
    posts = (d.get("platforms", {}).get("x") or {}).get("posts") or []
    return posts[0].get("text") if posts else None


def render(text, key):
    os.makedirs(CARDS, exist_ok=True)
    out = os.path.join(CARDS, key + ".png")
    r = subprocess.run([sys.executable, os.path.join(HERE, "render_story.py"),
                        "--no-open", "--text", text, "--out", CARDS],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError("render failed: " + r.stderr[-300:])
    produced = r.stdout.strip().split("\n")[-1]
    os.replace(produced, out)
    return out


def cdn_url(key):
    return "https://cdn.jsdelivr.net/gh/%s@main/cards/%s.png" % (REPO, key)


def wait_for_cdn(url):
    """jsDelivr caches on first hit and lags a fresh push. Handing Instagram a
    URL that still 404s makes the container fail with a useless error."""
    for _ in range(30):
        try:
            if urllib.request.urlopen(url, timeout=20).status == 200:
                return True
        except Exception:
            pass
        time.sleep(4)
    return False


def publish(url):
    c = ig_post("/%s/media" % IG_USER,
                {"image_url": url, "media_type": "STORIES", "access_token": IG_TOKEN})
    cid = c.get("id")
    if not cid:
        raise RuntimeError("no container: %s" % c)
    for _ in range(25):
        s = ig_get("/%s?fields=status_code&access_token=%s" % (cid, IG_TOKEN)).get("status_code")
        if s == "FINISHED":
            break
        if s == "ERROR":
            raise RuntimeError("container errored")
        time.sleep(3)
    else:
        raise RuntimeError("container never finished")
    r = ig_post("/%s/media_publish" % IG_USER, {"creation_id": cid, "access_token": IG_TOKEN})
    if not r.get("id"):
        raise RuntimeError("publish returned %s" % r)
    return r["id"]


def main():
    today = (sys.argv[sys.argv.index("--date") + 1]
             if "--date" in sys.argv else datetime.now(TZ).date().isoformat())
    for name, val in [("TYPEFULLY_API_KEY", TF_KEY), ("IG_USER_ID", IG_USER),
                      ("IG_ACCESS_TOKEN", IG_TOKEN)]:
        if not val and not DRY:
            sys.exit("missing required secret: " + name)

    done = json.load(open(STATE)) if os.path.exists(STATE) else []
    drafts = [d for d in todays_drafts(today) if str(d["id"]) not in map(str, done)]
    if not drafts:
        print("nothing to post for %s" % today)
        return

    failed = 0
    for d in drafts:
        did, title = d["id"], (d.get("draft_title") or str(d["id"]))[:60]
        try:
            text = draft_text(did)
            if not text:
                raise RuntimeError("draft has no X text")
            key = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:40] + "-" + today
            png = render(text, key)
            print("rendered %s (%d bytes)" % (os.path.basename(png), os.path.getsize(png)))
            if DRY:
                print("DRY RUN, not publishing:\n%s\n" % text)
                continue
            url = cdn_url(key)
            subprocess.run(["git", "add", "-A"], cwd=HERE, check=False)
            subprocess.run(["git", "-c", "user.name=story-bot",
                            "-c", "user.email=bot@users.noreply.github.com",
                            "commit", "-q", "-m", "card: " + key], cwd=HERE, check=False)
            subprocess.run(["git", "push", "-q"], cwd=HERE, check=False)
            if not wait_for_cdn(url):
                raise RuntimeError("CDN never served %s" % url)
            mid = publish(url)
            done.append(str(did))
            print("STORY PUBLISHED %s -> %s" % (title, mid))
        except Exception as ex:
            failed += 1
            print("FAIL %s: %s" % (title, str(ex)[:200]))
            slack(":x: IG story failed for *%s*: %s" % (title, str(ex)[:200]))

    if not DRY:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        json.dump(done, open(STATE, "w"), indent=1)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

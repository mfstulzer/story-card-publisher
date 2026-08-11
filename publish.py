#!/usr/bin/env python3
"""
Publish an Instagram story card for whatever is scheduled to post today.

Runs on GitHub Actions, so Mark's Mac can be asleep, off, or gone.

SOURCE OF TRUTH IS TYPEFULLY, NOT THE VAULT.
The vault file lives on one laptop and cannot be read from CI. Typefully already
knows what is scheduled and what text will actually go out, so this asks it
directly. That also means the story and the post can never show different words.

EVERY POST OF THE DAY GETS A CARD, BUT NEVER A THREAD.
Each run cards EVERY scheduled post of the day that has already gone out and has
not been carded yet -- not just the earliest. That covers the two posts the vault
tweet-queue claims AND the ones Mark writes straight into Typefully in the later
slots (Mark's call 2026-08-10).

THE "ALREADY GONE OUT" FILTER IS THE WHOLE SAFETY MECHANISM. A run only considers
drafts whose scheduled_date is in the past, so an 08:10 run cannot card the 11:00
post three hours early with a card timestamped 8:10 AM. Before 2026-08-10 that
guarantee came from one hand-tuned cron per expected slot, which meant every
change to the posting schedule silently broke coverage (it did, on 08-10, when
Sunday slots moved). Now the schedule is dumb -- hourly across the posting window
-- and the clock comparison decides what is ripe. Do NOT reintroduce per-slot
crons.

Threads are skipped rather than rendered, because the card only has room for one
tweet and a thread would post as its opening line with the rest silently missing.
A thread just means one fewer card that day (Mark's call 2026-08-10, asked again
when the cap came off).

MAX_PER_DAY is now a runaway backstop, not the operative limit -- it exists so a
bug in the date filter cannot dump twenty stories on a real account. A weekday
has five Typefully slots, so anything at or under that is normal traffic.

--probe prints how many posts are ripe and exits without rendering or
publishing. The workflow runs it first so the ten-odd no-op runs a day skip the
apt-get/pip install entirely. It calls the SAME selection code as a real run, so
it cannot drift out of agreement with it.

IDEMPOTENCY: published draft ids are appended to state/published.json and
committed back at the END of the run, in a SECOND commit. The first commit (mid
loop) exists only to get the card image into the repo so jsDelivr can serve it
to Instagram, and necessarily predates the publish, so it cannot carry the id.
A rerun on the same day is then a no-op. This matters more here than locally,
because Actions can retry a job -- and since 2026-08-10 there are two scheduled
runs a day, which without a persisted id would card the same post twice.

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
MAX_PER_DAY = 8     # runaway backstop only, NOT the intended volume (5 slots/weekday)
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
PROBE = "--probe" in sys.argv


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


def draft_posts(draft_id):
    """All X posts on the draft. Length > 1 means it is a thread."""
    d = tf("/drafts/%s" % draft_id)
    return (d.get("platforms", {}).get("x") or {}).get("posts") or []


def pick_all(drafts, cutoff):
    """Every single-tweet post of the day that has ALREADY gone out, oldest first.

    cutoff is the wall clock. A post still in the future is not skipped for good,
    it is simply not ripe yet -- a later run picks it up. That is what lets the
    cron schedule be dumb. Threads are skipped permanently; a card holds one tweet.
    """
    ripe = []
    for d in sorted(drafts, key=lambda r: r["scheduled_date"]):
        when = datetime.fromisoformat(d["scheduled_date"].replace("Z", "+00:00")).astimezone(TZ)
        if when > cutoff:
            print("not yet: %s posts at %s" % (d["id"], when.strftime("%H:%M")))
            continue
        posts = draft_posts(d["id"])
        if not posts:
            print("skip %s: no X text" % d["id"])
            continue
        if len(posts) > 1:
            print("skip %s: thread (%d tweets), a card only holds one"
                  % (d["id"], len(posts)))
            continue
        ripe.append((d, posts[0].get("text")))
    return ripe


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


def probe_out(n):
    """Tell the workflow whether the expensive steps are worth running."""
    out = os.environ.get("GITHUB_OUTPUT")
    if PROBE and out:
        with open(out, "a") as fh:
            fh.write("ripe=%d\n" % n)
    return None


def main():
    today = (sys.argv[sys.argv.index("--date") + 1]
             if "--date" in sys.argv else datetime.now(TZ).date().isoformat())
    for name, val in [("TYPEFULLY_API_KEY", TF_KEY), ("IG_USER_ID", IG_USER),
                      ("IG_ACCESS_TOKEN", IG_TOKEN)]:
        if not val and not DRY:
            sys.exit("missing required secret: " + name)

    # A real run cards what has already posted. A manual --date backfill of a past
    # day has no "now" to compare against, so the whole of that day counts as ripe.
    now = datetime.now(TZ)
    cutoff = (now if today == now.date().isoformat()
              else datetime.fromisoformat(today + "T23:59:59").replace(tzinfo=TZ))

    done = json.load(open(STATE)) if os.path.exists(STATE) else []
    done_str = set(map(str, done))

    all_today = todays_drafts(today)          # paginates; do it once
    if not all_today:
        print("nothing scheduled for %s" % today)
        return probe_out(0)
    already = sum(1 for d in all_today if str(d["id"]) in done_str)
    if already >= MAX_PER_DAY:
        # Not a normal ceiling. Five slots a weekday means hitting 8 is a bug.
        print("%d card(s) already went out today, backstop is %d" % (already, MAX_PER_DAY))
        slack(":warning: IG story backstop hit: %d cards already today (%s). "
              "Expected at most one per Typefully slot -- check the date filter."
              % (already, today))
        return probe_out(0)

    pending = pick_all([d for d in all_today if str(d["id"]) not in done_str], cutoff)
    if not pending:
        print("nothing ripe to card yet for %s (%d scheduled today, %d already carded)"
              % (today, len(all_today), already))
        return probe_out(0)

    room = MAX_PER_DAY - already
    if len(pending) > room:
        print("trimming %d ripe post(s) to %d, backstop" % (len(pending), room))
        pending = pending[:room]
    print("carding %d post(s) for %s" % (len(pending), today))
    if PROBE:
        return probe_out(len(pending))

    failed = 0
    for d, text in pending:
        did, title = d["id"], (d.get("draft_title") or str(d["id"]))[:60]
        try:
            # Draft id is in the key because two posts on one day can share a
            # title, and a colliding filename would overwrite the earlier card.
            key = (re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:40]
                   + "-" + today + "-" + str(did))
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
        # COMMIT AGAIN, deliberately. The commit inside the loop happens BEFORE
        # publish() because jsDelivr can only serve the card once it is in the
        # repo, so that commit cannot possibly carry the published id -- it is
        # not known until the story actually lands. Writing the state after it
        # and never committing is why published.json sat at [] from 8-05 to
        # 8-10: every card was committed, no id ever was. Harmless while there
        # was one run a day; at the current hourly cadence it would re-card the
        # same post on every run for the rest of the day.
        subprocess.run(["git", "add", STATE], cwd=HERE, check=False)
        subprocess.run(["git", "-c", "user.name=story-bot",
                        "-c", "user.email=bot@users.noreply.github.com",
                        "commit", "-q", "-m",
                        "state: %d card(s) recorded through %s" % (len(done), today)],
                       cwd=HERE, check=False)
        subprocess.run(["git", "push", "-q"], cwd=HERE, check=False)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

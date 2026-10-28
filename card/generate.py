#!/usr/bin/env python3
"""Render the neofetch-style profile card (card/dark.svg and card/light.svg).

    python card/generate.py                       # fetch stats from GitHub, then render
    python card/generate.py --stats stats.json    # render from saved stats (no network)
    python card/generate.py --print-stats         # also print the stats as JSON

Standard library only. Needs a token in GITHUB_TOKEN for the API (the workflow's
built-in token is enough for public data; a personal token in the STATS_TOKEN
secret would also count private repositories).

Stats:
  Repos        repositories you own (forks included); Contributed = other repos you
               committed to / opened PRs in
  Stars        stars on the repositories you own
  Followers    GitHub followers
  Commits      commit contributions, all years (what your contribution graph counts)
  Lines of Code  additions / deletions authored by you in your own non-fork repos,
               from GitHub's contributor statistics. Cached in card/cache.json, so a
               repository whose statistics are still being computed keeps its last value.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
API = "https://api.github.com"

# ---------------------------------------------------------------- layout ----
WIDTH_CHARS = 62          # info column width in characters
INFO_SIZE = 16            # info font size (px)
INFO_LINE = 20            # info line height (px)
CHAR_EM = 0.61            # widest common monospace advance (Courier/DejaVu ~0.60)
PAD = 32                  # outer padding (px)
GAP = 36                  # gap between portrait and info (px)
FONT = "Consolas, 'DejaVu Sans Mono', 'Liberation Mono', Menlo, 'Courier New', monospace"

THEMES = {
    "dark": {
        "bg": "#040a06", "border": "#1d4a2a", "art": "#4fb872", "key": "#7cffa0",
        "value": "#e4efe9", "dots": "#2f6b42", "title": "#7cffa0", "rule": "#2f6b42",
        "add": "#7cffa0", "del": "#ff7a45", "scan": 0.22,
    },
    "light": {
        "bg": "#f6f8f4", "border": "#cfdcd2", "art": "#2f6b45", "key": "#1a7f37",
        "value": "#1f2328", "dots": "#9db0a3", "title": "#1a7f37", "rule": "#9db0a3",
        "add": "#1a7f37", "del": "#cf222e", "scan": 0.0,
    },
}


# ------------------------------------------------------------------ data ----
def api(path: str, token: str, body: dict | None = None, accept_202: bool = False):
    """GET/POST the GitHub API. Returns (status, json)."""
    req = urllib.request.Request(
        path if path.startswith("http") else API + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "profile-card",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            raw = res.read()
            return res.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as err:
        if accept_202 and err.code == 202:
            return 202, None
        raise RuntimeError(f"GitHub API {path}: HTTP {err.code} {err.read()[:300]!r}") from err


def graphql(query: str, variables: dict, token: str) -> dict:
    _, data = api("/graphql", token, {"query": query, "variables": variables})
    if data.get("errors"):
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data["data"]


USER_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    createdAt
    followers { totalCount }
    contributionsCollection { contributionYears }
    repositoriesContributedTo(
      includeUserRepositories: false
      contributionTypes: [COMMIT, PULL_REQUEST, REPOSITORY]
    ) { totalCount }
    repositories(ownerAffiliations: OWNER, first: 100, after: $cursor) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { nameWithOwner isFork stargazerCount pushedAt }
    }
  }
}"""


def fetch_stats(login: str, token: str, cache: dict) -> dict:
    repos, cursor = [], None
    while True:
        user = graphql(USER_QUERY, {"login": login, "cursor": cursor}, token)["user"]
        repos += user["repositories"]["nodes"]
        page = user["repositories"]["pageInfo"]
        if not page["hasNextPage"]:
            break
        cursor = page["endCursor"]

    years = user["contributionsCollection"]["contributionYears"]
    fields = " ".join(
        f'y{y}: contributionsCollection(from: "{y}-01-01T00:00:00Z", to: "{y}-12-31T23:59:59Z")'
        " { totalCommitContributions }"
        for y in years
    )
    yearly = graphql(f"query($login: String!) {{ user(login: $login) {{ {fields} }} }}", {"login": login}, token)["user"]
    commits = sum(v["totalCommitContributions"] for v in yearly.values())

    loc_cache = cache.setdefault("loc", {})
    for repo in repos:
        if repo["isFork"]:
            continue
        name = repo["nameWithOwner"]
        cached = loc_cache.get(name)
        if cached and cached.get("pushedAt") == repo["pushedAt"]:
            continue  # nothing pushed since the last count
        counted = repo_loc(name, login, token)
        if counted is not None:
            loc_cache[name] = {"pushedAt": repo["pushedAt"], **counted}
        elif not cached:
            print(f"  ! {name}: statistics not ready yet, counted as 0 for now", file=sys.stderr)
    live = {r["nameWithOwner"] for r in repos if not r["isFork"]}
    for name in list(loc_cache):
        if name not in live:
            del loc_cache[name]  # deleted / now a fork / transferred

    return {
        "created_at": user["createdAt"],
        "repos": user["repositories"]["totalCount"],
        "contributed": user["repositoriesContributedTo"]["totalCount"],
        "stars": sum(r["stargazerCount"] for r in repos),
        "followers": user["followers"]["totalCount"],
        "commits": commits,
        "additions": sum(v["additions"] for v in loc_cache.values()),
        "deletions": sum(v["deletions"] for v in loc_cache.values()),
    }


def repo_loc(name: str, login: str, token: str) -> dict | None:
    """Additions/deletions by `login` in one repo. None if GitHub is still computing."""
    for attempt in range(6):
        status, data = api(f"/repos/{name}/stats/contributors", token, accept_202=True)
        if status == 200:
            for entry in data or []:
                author = (entry.get("author") or {}).get("login", "")
                if author.lower() == login.lower():
                    return {
                        "additions": sum(w["a"] for w in entry["weeks"]),
                        "deletions": sum(w["d"] for w in entry["weeks"]),
                    }
            return {"additions": 0, "deletions": 0}  # no commits by this user
        if status == 204:
            return {"additions": 0, "deletions": 0}  # empty repository
        time.sleep(3 + attempt * 2)  # 202: statistics are being computed
    return None


BULK_DIRS = {
    "node_modules": "node_modules", "bower_components": "bower_components", "vendor": "vendor",
    "dist": "dist", "build": "build", ".next": ".next", "out": "out",
    "Library": "Unity Library", "Temp": "Unity Temp", "obj": "obj", "bin": "bin",
    "venv": "venv", ".venv": ".venv", "env": "env", "site-packages": "site-packages",
}


def committed_bulk(name: str, token: str) -> list[str]:
    """Folders in the default branch that usually hold dependencies or generated files."""
    _, repo = api(f"/repos/{name}", token)
    _, tree = api(f"/repos/{name}/git/trees/{repo['default_branch']}?recursive=1", token)
    seen: dict[str, int] = {}
    for item in tree.get("tree", []):
        parts = item["path"].split("/")
        for part in parts[:-1]:
            if part in BULK_DIRS:
                seen[BULK_DIRS[part]] = seen.get(BULK_DIRS[part], 0) + 1
                break
    unity_assets = sum(1 for i in tree.get("tree", []) if i["path"].endswith((".unity", ".prefab", ".asset", ".meta")))
    out = [f"{label} ({n} files)" for label, n in sorted(seen.items(), key=lambda kv: -kv[1])]
    if unity_assets:
        out.append(f"Unity asset/meta files ({unity_assets})")
    if tree.get("truncated"):
        out.append("tree truncated (very large)")
    return out


# ---------------------------------------------------------------- render ----
def add_months(day: dt.date, months: int) -> dt.date:
    """`day` moved by whole months, clamped to the end of shorter months (Jan 31 + 1 = Feb 28)."""
    y, m = divmod(day.month - 1 + months, 12)
    year, month = day.year + y, m + 1
    next_first = dt.date(year + (month == 12), month % 12 + 1, 1)
    return dt.date(year, month, min(day.day, (next_first - dt.timedelta(days=1)).day))


def uptime(since: dt.date, today: dt.date) -> str:
    """'X years, Y months, Z days' from `since` to `today` (same rules as dateutil's relativedelta)."""
    months = (today.year - since.year) * 12 + today.month - since.month
    if add_months(since, months) > today:
        months -= 1
    days = (today - add_months(since, months)).days
    part = lambda n, word: f"{n} {word}{'' if n == 1 else 's'}"
    return ", ".join([part(months // 12, "year"), part(months % 12, "month"), part(days, "day")])


def num(n) -> str:
    return f"{n:,}" if isinstance(n, int) else str(n)


def leader(key: str, value: str, width: int) -> list[tuple[str, str]]:
    """'. Key: ........ value' as (style, text) runs, exactly `width` characters."""
    head = f". {key}:"
    dots = width - len(head) - len(value) - 2
    if dots < 2:
        raise ValueError(f"Row too long for the card ({len(head) + len(value) + 4} > {width}): {key} = {value}")
    return [("dots", ". "), ("key", f"{key}:"), ("dots", " " + "." * dots + " "), ("value", value)]


def rule(title: str, first: bool) -> list[tuple[str, str]]:
    """Section header. The horizontal line ("hr") is drawn as an SVG line, not as
    dash characters, so it never depends on the viewer's font having that glyph."""
    if first:
        return [("title", title), ("hr", "")]
    return [("rule", "- "), ("title", title), ("hr", "")]


def stats_rows(s: dict, width: int) -> list[list[tuple[str, str]]]:
    left_w = 36
    right_w = width - left_w - 3

    def pair(lk, lv, rk, rv):
        left = leader(lk, lv, left_w)
        right = leader(rk, rv, right_w + 2)[1:]  # drop the leading '. '
        return left + [("dots", " | ")] + right

    rows = [
        pair("Repos", f"{num(s['repos'])} {{Contributed: {num(s['contributed'])}}}", "Stars", num(s["stars"])),
        pair("Commits", num(s["commits"]), "Followers", num(s["followers"])),
    ]
    adds, dels = s["additions"], s["deletions"]
    total = adds - dels if isinstance(adds, int) and isinstance(dels, int) else "…"
    tail = [("value", " ( "), ("add", f"{num(adds)}++"), ("value", ", "), ("del", f"{num(dels)}--"), ("value", " )")]
    tail_len = sum(len(t) for _, t in tail)
    try:
        loc = leader("Lines of Code on GitHub", num(total), width - tail_len)
    except ValueError:  # very large numbers: shorter label so the row still fits
        loc = leader("Lines of Code", num(total), width - tail_len)
    rows.append(loc + tail)
    return rows


def build_rows(profile: dict, stats: dict, today: dt.date) -> list[list[tuple[str, str]]]:
    since_cfg = profile.get("uptime_since", "github")
    since = (
        dt.date.fromisoformat(stats["created_at"][:10]) if since_cfg == "github" and stats.get("created_at")
        else dt.date.fromisoformat(since_cfg) if since_cfg != "github" else None
    )
    up = uptime(since, today) if since else "…"

    rows: list[list[tuple[str, str]]] = []
    for i, section in enumerate(profile["section"]):
        if i == 0:
            rows.append(rule(profile["title"], first=True))
        else:
            rows.append([])
            rows.append(rule(section["title"], first=False))
        if section.get("stats"):
            rows += stats_rows(stats, WIDTH_CHARS)
            continue
        for row in section["rows"]:
            if row == "":
                rows.append([("dots", ". ")])
            else:
                key, value = row
                rows.append(leader(key, value.replace("{uptime}", up), WIDTH_CHARS))
    return rows


def svg(art: list[str], rows: list[list[tuple[str, str]]], theme: dict, mode: str) -> str:
    info_h = len(rows) * INFO_LINE
    art_line = info_h / len(art)                    # fit the portrait to the info height
    art_size = round(art_line / 1.2, 2)
    art_w = max(len(l) for l in art) * art_size * CHAR_EM
    info_x = PAD + art_w + GAP
    width = round(info_x + WIDTH_CHARS * INFO_SIZE * CHAR_EM + PAD)
    height = round(info_h + 2 * PAD)
    t = theme

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"'
        f' font-family="{html.escape(FONT)}" role="img" aria-labelledby="t">',
        f"<title id=\"t\">Profile card ({mode} mode)</title>",
        "<defs>",
        f'<pattern id="scan" width="4" height="3" patternUnits="userSpaceOnUse">'
        f'<rect y="2" width="4" height="1" fill="#000" fill-opacity="{t["scan"]}"/></pattern>',
        "</defs>",
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="14" fill="{t["bg"]}" stroke="{t["border"]}"/>',
    ]
    # Every line is its own <text> with an exact textLength: whatever monospace font
    # the viewer has (or a fallback glyph it substitutes), the browser adjusts the
    # spacing so each row is exactly as wide as computed and the columns line up.
    art_adv = art_size * CHAR_EM
    out.append(f'<g font-size="{art_size}" fill="{t["art"]}">')
    for i, line in enumerate(art):
        if not line.strip():
            continue
        y = PAD + art_line * (i + 0.8)
        out.append(
            f'<text x="{PAD}" y="{y:.2f}" textLength="{len(line) * art_adv:.2f}" lengthAdjust="spacing"'
            f' xml:space="preserve">{html.escape(line)}</text>'
        )
    out.append("</g>")
    info_adv = INFO_SIZE * CHAR_EM
    out.append(f'<g font-size="{INFO_SIZE}">')
    for i, runs in enumerate(rows):
        if not runs:
            continue
        y = PAD + INFO_LINE * (i + 0.8)
        text_runs = [(style, text) for style, text in runs if style != "hr"]
        length = sum(len(text) for _, text in text_runs)
        spans = "".join(f'<tspan fill="{t[style]}">{html.escape(text)}</tspan>' for style, text in text_runs)
        out.append(
            f'<text x="{info_x:.1f}" y="{y:.1f}" textLength="{length * info_adv:.1f}" lengthAdjust="spacing"'
            f' xml:space="preserve">{spans}</text>'
        )
        if len(text_runs) < len(runs):  # section rule: line from after the title to the right edge
            x1, x2, ly = info_x + (length + 1) * info_adv, info_x + WIDTH_CHARS * info_adv, y - INFO_SIZE * 0.32
            out.append(f'<line x1="{x1:.1f}" y1="{ly:.1f}" x2="{x2:.1f}" y2="{ly:.1f}" stroke="{t["rule"]}" stroke-width="1.5"/>')
    out.append("</g>")
    if t["scan"]:
        out.append(f'<rect x="1" y="1" width="{width - 2}" height="{height - 2}" rx="13" fill="url(#scan)" pointer-events="none"/>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ main ----
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", help="render from this stats JSON instead of calling the API")
    ap.add_argument("--print-stats", action="store_true", help="print the stats as one JSON line")
    ap.add_argument("--loc-report", action="store_true", help="list lines of code per repo from card/cache.json")
    args = ap.parse_args()

    if args.loc_report:
        loc = json.loads((HERE / "cache.json").read_text()).get("loc", {})
        ranked = sorted(loc.items(), key=lambda kv: -kv[1]["additions"])
        lines = [f"{name}: +{v['additions']:,} -{v['deletions']:,}" for name, v in ranked if v["additions"]]
        print("::notice title=Lines of code by repository::" + " | ".join(lines))
        token = os.environ.get("GITHUB_TOKEN")
        if token:  # what's committed in the biggest repos (dependencies / build output / engine files)
            found = [f"{name}: {', '.join(committed_bulk(name, token)) or 'nothing unusual'}" for name, _ in ranked[:15]]
            print("::notice title=Committed dependency or generated folders::" + " | ".join(found))
        return 0

    profile = tomllib.loads((HERE / "profile.toml").read_text(encoding="utf-8"))
    art = (HERE / "portrait.txt").read_text(encoding="utf-8").rstrip("\n").split("\n")
    cache_path = HERE / "cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    if args.stats:
        stats = json.loads(Path(args.stats).read_text())
    else:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            print("GITHUB_TOKEN is not set", file=sys.stderr)
            return 1
        stats = fetch_stats(profile["github"], token, cache)
        cache_path.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n")

    if args.print_stats:
        print(json.dumps(stats, sort_keys=True))

    today = dt.datetime.now(dt.timezone.utc).date()
    rows = build_rows(profile, stats, today)
    for mode, theme in THEMES.items():
        (HERE / f"{mode}.svg").write_text(svg(art, rows, theme, mode), encoding="utf-8")
    print(f"rendered card/dark.svg and card/light.svg ({len(rows)} rows)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

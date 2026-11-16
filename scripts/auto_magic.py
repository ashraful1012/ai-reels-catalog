"""
Kahini auto-update: finds new magical AI full movies on YouTube and adds them to catalog.json.
Runs every day from GitHub Actions (.github/workflows/auto-magic.yml). No server, no manual work.

What it does
  1. Searches YouTube for fantasy short-drama full movies (werewolf, dragon, vampire, magic, mermaid ...).
  2. Keeps only full movies (40+ min) from channels already vetted, or that mark themselves as AI.
  3. Drops other platforms' shows re-posted by third parties, and stories posted on several channels.
  4. Drops videos whose frame matches a video already in the list (copies under a new title).
  5. Keeps only public videos that can be embedded (YouTube oEmbed answers 200).
  6. Adds at most MAX_NEW per day, best-watched first, and bumps "version" so phones update.
  7. Moves videos that were deleted or blocked into "remove" so the app hides them.

usage:  python scripts/auto_magic.py [--dry-run]
"""
import json, re, sys, time, datetime, urllib.request, urllib.parse, io, os

CATALOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'catalog.json')
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state.json')
MAX_NEW = 10
DRY = '--dry-run' in sys.argv

QUERIES = [
    'lycan king full movie', 'alpha king full movie ai drama', 'werewolf luna full movie ai', 'wolf queen full movie ai drama',
    'dragon king full movie ai drama', 'dragon twins full movie ai', 'baby dragon full movie ai', 'vampire king full movie ai drama',
    'vampire queen full movie ai', 'magic academy full movie ai drama', 'witch full movie ai drama', 'archmage full movie ai drama',
    'mermaid princess full movie ai drama', 'sea king full movie ai drama', 'fae prince full movie ai', 'elf prince full movie ai drama',
    'phoenix full movie ai drama', 'AIStory full movie', 'aidrama full movie fantasy', 'ai short drama full movie werewolf',
]
FANT = re.compile(r'(?i)wolf|lycan|alpha|luna|dragon|vampire|witch|magic|mermaid|\bfae\b|\belf\b|elven|phoenix|mage|wizard|sea king|ocean king|spell')
AI_MARK = re.compile(r'(?i)#?ai ?story|#?ai ?drama|\bai\b|a\.i\.')
REUP = re.compile(r'(?i)#?(dramabox|netshort|goodshort|reelshort|shortmax|flextv|dramawave|moboreels|kalos|snackshort|minishort|cdrama|c-drama|'
                  r'chinese drama|kdrama|bl\b|male preg)|1-100|1~|part ?1\b|all episodes|[一-鿿]')
SKIP_CH = re.compile(r'(?i)barbie|unicorn academy|hollywood|movie time|indie rights|films for family|news|facts|islamic|badshah|one heart studio')
SKIP_TITLE = re.compile(r"(?i)fae prince.*village girl|mermaid'?s tear|bride they gave the beast")   # removed on request / known re-posts
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36',
      'Accept-Language': 'en-US,en;q=0.9', 'Cookie': 'CONSENT=YES+1; SOCS=CAI'}


def get(url, binary=False, timeout=30):
    data = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()
    return data if binary else data.decode('utf-8', 'replace')


def walk(o, key):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key: yield v
            yield from walk(v, key)
    elif isinstance(o, list):
        for v in o: yield from walk(v, key)


def txt(o):
    if not o: return ''
    if isinstance(o, str): return o
    if 'simpleText' in o: return o['simpleText']
    if 'runs' in o: return ''.join(r.get('text', '') for r in o['runs'])
    return ''


def search(q):
    html = get('https://www.youtube.com/results?hl=en&search_query=' + urllib.parse.quote(q) + '&sp=EgIQAQ%253D%253D')
    m = re.search(r'var ytInitialData = (\{.*?\});</script>', html)
    if not m: return []
    out = []
    for r in walk(json.loads(m.group(1)), 'videoRenderer'):
        d = txt(r.get('lengthText')).split(':')
        mins = int(d[0]) * 60 + int(d[1]) if len(d) == 3 else (int(d[0]) if d[0].isdigit() else 0)
        views = int(re.sub(r'\D', '', txt(r.get('viewCountText')) or '0') or 0)
        out.append({'v': r.get('videoId'), 't': txt(r.get('title')), 'ch': txt(r.get('ownerText')).strip(), 'min': mins, 'views': views})
    return out


def embeddable(v):
    try:
        get('https://www.youtube.com/oembed?format=json&url=https://www.youtube.com/watch?v=' + v, timeout=20)
        return True
    except urllib.error.HTTPError as e:
        return False if e.code in (401, 403, 404) else None    # None = unknown (network), keep as is
    except Exception:
        return None


def stem(t):
    t = re.sub(r'【[^】]*】|\[[^\]]*\]|\([^)]*\)|#\S+|[^\w\s]', ' ', t.lower())
    stop = {'full', 'movie', 'episode', 'eng', 'sub', 'dub', 'hd', 'new', 'the', 'a', 'her', 'his', 'version', 'complete'}
    return ' '.join(w for w in t.split() if w not in stop)[:40]


def clean(t):
    t = re.sub(r'【[^】]*】|\[[^\]]*\]|「[^」]*」|#\S+|\|.*$', ' ', t)
    t = re.sub(r'(?i)\b(full movie|full episode|full version|full|eng ?sub|eng ?dub|engsub|hd|4k|new|multi sub|complete|movies?)\b', ' ', t)
    t = re.sub(r"[^\w\s’'.,!?—–:;&-]", ' ', t)
    return re.sub(r'\s+', ' ', t).strip(' -–—|:')[:110]


def genre(t):
    t = t.lower(); g = ['fantasy']
    for k, rx in (('werewolf', r'wolf|lycan|alpha|luna|pup'), ('dragon', r'dragon'), ('vampire', r'vampire'),
                  ('magic', r'witch|magic|mage|wizard|spell|academy'), ('mermaid', r'mermaid|sea king|ocean')):
        if re.search(rx, t): g.append(k); break
    g.append('revenge' if re.search(r'betray|revenge|kneel|regret|reject|humiliat|bull|abandon', t) else 'romance')
    return ','.join(g)


def frame_hash(v):
    """16x16 average hash of a frame from the middle of the video (needs Pillow; None if unavailable)."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(get(f'https://i.ytimg.com/vi/{v}/hq2.jpg', binary=True))).convert('L')
        w, h = im.size
        im = im.crop((w // 8, h // 8, w * 7 // 8, h * 7 // 8)).resize((16, 16))
        px = list(im.getdata()); avg = sum(px) / len(px)
        return ''.join('1' if p > avg else '0' for p in px)
    except Exception:
        return None


def dist(a, b):
    return sum(x != y for x, y in zip(a, b)) if a and b else 999


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    cat = json.load(open(CATALOG, encoding='utf-8'))
    state = json.load(open(STATE, encoding='utf-8')) if os.path.exists(STATE) else {}
    allow = set(state.get('channels', []))
    hashes = state.get('hashes', {})
    have = {m[0] for m in cat['movies']} | {e[0] for s in cat['series'] for e in s.get('e', [])} | set(state.get('rejected', []))
    stems = {stem(m[1]) for m in cat['movies']}

    # 1-3: search and filter
    found = {}
    for q in QUERIES:
        try: res = search(q)
        except Exception as e: print('search failed', q, e); time.sleep(5); continue
        for r in res:
            if not r['v'] or r['v'] in have or r['v'] in found: continue
            if r['min'] < 40 or not FANT.search(r['t']) or REUP.search(r['t']) or REUP.search(r['ch']) or SKIP_CH.search(r['ch']) or SKIP_TITLE.search(r['t']): continue
            if r['ch'] not in allow and not (AI_MARK.search(r['t']) or AI_MARK.search(r['ch'])): continue
            found[r['v']] = r
        time.sleep(2)
    by_stem = {}
    for r in found.values(): by_stem.setdefault(stem(r['t']), []).append(r)
    cands = [L[0] for s, L in by_stem.items() if len(L) == 1 and s not in stems]   # one channel only, not already listed
    cands.sort(key=lambda r: -r['views'])
    print(len(found), 'found,', len(cands), 'after duplicate-story filter')

    # 4-5: frame duplicates and embeddability
    for m in cat['movies']:
        if m[0] not in hashes and len(hashes) < 5000:
            h = frame_hash(m[0])
            if h: hashes[m[0]] = h
    added = []
    for r in cands:
        if len(added) >= MAX_NEW: break
        h = frame_hash(r['v'])
        if h and any(dist(h, x) <= 12 for x in hashes.values()):
            print('  copy of a listed video:', r['v'], r['t'][:60]); state.setdefault('rejected', []).append(r['v']); continue
        if embeddable(r['v']) is not True:
            print('  not embeddable:', r['v'], r['t'][:60]); continue
        if h: hashes[r['v']] = h
        added.append([r['v'], clean(r['t']) or r['t'][:100], r['views'], r['min'], genre(r['t']), 'thumb'])
        allow.add(r['ch'])
        print('  + ', r['v'], r['min'], 'min |', r['ch'], '|', clean(r['t']))

    # 7: hide videos that were deleted or blocked since they were added
    removed = []
    for m in cat['movies']:
        if 'm-' + m[0] not in cat.get('remove', []) and embeddable(m[0]) is False:
            removed.append('m-' + m[0])
    if removed: print('  removing', len(removed), 'dead videos')

    if not added and not removed:
        print('nothing new today'); return
    cat['movies'] += added
    cat['remove'] = sorted(set(cat.get('remove', []) + removed))
    cat['version'] = int(cat.get('version', 0)) + 1
    cat['updated'] = datetime.date.today().isoformat()
    state.update({'channels': sorted(allow), 'hashes': hashes, 'rejected': state.get('rejected', [])[-2000:]})
    if DRY:
        print('dry run: would write version', cat['version'], 'with', len(added), 'new,', len(removed), 'removed'); return
    json.dump(cat, open(CATALOG, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    json.dump(state, open(STATE, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    print('catalog version', cat['version'], ':', len(added), 'new,', len(removed), 'removed')


if __name__ == '__main__':
    main()

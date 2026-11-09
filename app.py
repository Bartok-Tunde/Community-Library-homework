"""Community Library - Flask + SQLite. Run: pip install -r requirements.txt && python app.py"""
import calendar as pycal, json, math, os, secrets, sqlite3, time, urllib.parse, urllib.request, uuid
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from functools import wraps
from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "library.db")
UPLOADS = os.path.join(BASE, "static", "uploads")
THEMES = ["Romance", "Historical", "Literature", "Dictionaries", "Fantasy", "Science fiction",
          "Mystery & crime", "Biography", "Children", "Poetry", "Science", "Cooking", "Travel", "Textbooks"]
LANGS = ["English", "Hungarian", "German", "French", "Spanish", "Italian", "Russian", "Other"]
METHODS = ["Borrow", "Swap", "Keep"]
LANG_CODES = {"English": "eng", "Hungarian": "hun", "German": "ger", "French": "fre",
              "Spanish": "spa", "Italian": "ita", "Russian": "rus"}   # Open Library language codes
UA = {"User-Agent": "CommunityLibrary/0.1 (self-hosted)"}

# ---------- database ----------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d: d.close()

def init_db():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE, pw TEXT,
        name TEXT, photo TEXT, have TEXT DEFAULT '[]', seek TEXT DEFAULT '[]', languages TEXT DEFAULT '[]');
    CREATE TABLE IF NOT EXISTS locations(id INTEGER PRIMARY KEY, user_id INT, name TEXT,
        address TEXT, lat REAL, lon REAL);
    CREATE TABLE IF NOT EXISTS books(id INTEGER PRIMARY KEY, user_id INT, title TEXT, author TEXT,
        language TEXT, year TEXT, info TEXT, cover TEXT, theme TEXT, methods TEXT, location_id INT);
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, sender INT, recipient INT,
        body TEXT, ts REAL, is_read INTEGER DEFAULT 0, loan_id INTEGER);
    CREATE TABLE IF NOT EXISTS loans(id INTEGER PRIMARY KEY, book_id INT, title TEXT, lender_id INT,
        borrower_id INT, start_date TEXT, due_date TEXT, status TEXT, proposed_by INT, created REAL);
    """)
    cols = [r[1] for r in c.execute("PRAGMA table_info(messages)")]
    if "is_read" not in cols:                      # upgrade an existing database
        c.execute("ALTER TABLE messages ADD COLUMN is_read INTEGER DEFAULT 0")
        c.execute("UPDATE messages SET is_read=1")  # old messages count as already read
    if "loan_id" not in cols:
        c.execute("ALTER TABLE messages ADD COLUMN loan_id INTEGER")
    if "cal_token" not in [r[1] for r in c.execute("PRAGMA table_info(users)")]:
        c.execute("ALTER TABLE users ADD COLUMN cal_token TEXT")
    if "languages" not in [r[1] for r in c.execute("PRAGMA table_info(users)")]:
        c.execute("ALTER TABLE users ADD COLUMN languages TEXT DEFAULT '[]'")
    c.commit(); c.close()

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if "uid" not in session: return redirect(url_for("home"))
        return f(*a, **k)
    return w

def me():
    return db().execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()

@app.context_processor
def inject():
    u = me() if "uid" in session else None
    unread = 0
    if u:
        unread = db().execute("SELECT COUNT(*) FROM messages WHERE recipient=? AND is_read=0", (u["id"],)).fetchone()[0]
    return dict(user=u, unread=unread, THEMES=THEMES, LANGS=LANGS, METHODS=METHODS, display_name=display_name)

def display_name(u):
    return (u["name"] or "").strip() or "Anonymous"

def save_photo(f):
    if not f or not f.filename: return None
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"): return None
    name = uuid.uuid4().hex + ext
    f.save(os.path.join(UPLOADS, name))
    return name

# ---------- external data (Open Library, Nominatim) ----------
def fetch_json(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=8) as r:
            return json.load(r)
    except Exception:
        return None

def ol_search(q, limit=8, field="q", strict=False):
    """strict=True returns None (instead of []) when Open Library can't be reached."""
    url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode(
        {field: q, "limit": limit, "fields": "title,author_name,first_publish_year,language,cover_i,subject"})
    data = fetch_json(url)
    if data is None and strict: return None
    data = data or {}
    out = []
    for d in data.get("docs", []):
        out.append(dict(title=d.get("title", ""), author=(d.get("author_name") or [""])[0],
                        year=d.get("first_publish_year") or "",
                        cover=f"https://covers.openlibrary.org/b/id/{d['cover_i']}-M.jpg" if d.get("cover_i") else "",
                        subjects=(d.get("subject") or [])[:5], languages=d.get("language") or []))
    return out

def geocode(addr):
    data = fetch_json("https://nominatim.openstreetmap.org/search?" +
                      urllib.parse.urlencode({"q": addr, "format": "json", "limit": 1}))
    time.sleep(1)  # Nominatim usage policy: max 1 request/second
    if data:
        return float(data[0]["lat"]), float(data[0]["lon"]), data[0]["display_name"]
    return None

def haversine(a, b, c, d):
    p = math.pi / 180
    x = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(x))

def wild(q):
    """'rick rio' -> 'rick rio*' so the word still being typed matches as a prefix."""
    w = q.split()
    if w and len(w[-1]) >= 2: w[-1] += "*"
    return " ".join(w)

@app.route("/api/lookup")
@login_required
def api_lookup():
    """Title suggestions while typing."""
    q = request.args.get("q", "").strip()
    if len(q) < 2: return jsonify(error=False, items=[])
    res = ol_search(wild(q), 10, "title", strict=True)
    if res == []: res = ol_search(q, 10, "title", strict=True)   # fall back to whole-word search
    return jsonify(error=res is None, items=res or [])

@app.route("/api/authors")
@login_required
def api_authors():
    """?title=... -> authors of that title (auto mode); ?q=... -> authors starting like the typed text (manual mode)."""
    title, q = request.args.get("title", "").strip(), request.args.get("q", "").strip()
    names = []
    if title:
        res = ol_search(title, 10, "title", strict=True)
        err = res is None
        for r in res or []:
            if r["author"] and r["author"] not in names: names.append(r["author"])
    elif len(q) >= 2:
        url = "https://openlibrary.org/search/authors.json?"
        data = fetch_json(url + urllib.parse.urlencode({"q": wild(q), "limit": 10}))
        if data is not None and not data.get("docs"):                 # fall back to whole-word search
            data = fetch_json(url + urllib.parse.urlencode({"q": q, "limit": 10}))
        err = data is None
        names = [d["name"] for d in (data or {}).get("docs", []) if d.get("name")]
    else:
        return jsonify(error=False, items=[])
    return jsonify(error=err, items=names)

# ---------- auth ----------
@app.route("/", methods=["GET", "POST"])
def home():
    if "uid" in session: return redirect(url_for("search"))
    if request.method == "POST":
        email, pw = request.form["email"].strip().lower(), request.form["password"]
        row = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if request.form["action"] == "register":
            if row or not email or len(pw) < 6:
                flash("That email is taken, or the password is shorter than 6 characters.")
            else:
                cur = db().execute("INSERT INTO users(email,pw) VALUES(?,?)", (email, generate_password_hash(pw)))
                db().commit(); session["uid"] = cur.lastrowid
                return redirect(url_for("profile"))
        elif row and check_password_hash(row["pw"], pw):
            session["uid"] = row["id"]; return redirect(url_for("search"))
        else:
            flash("Email or password is incorrect.")
    return render_template("home.html")

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("home"))

# ---------- profile ----------
@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    d = db()
    if request.method == "POST":
        act = request.form.get("action")
        if act == "save":
            photo = save_photo(request.files.get("photo")) or me()["photo"]
            d.execute("UPDATE users SET name=?, photo=?, have=?, seek=?, languages=? WHERE id=?",
                      (request.form.get("name", "").strip(), photo,
                       json.dumps(request.form.getlist("have")), json.dumps(request.form.getlist("seek")),
                       json.dumps([l for l in request.form.getlist("languages") if l in LANGS]), session["uid"]))
        elif act == "add_loc":
            g_ = geocode(request.form["address"])
            if g_:
                d.execute("INSERT INTO locations(user_id,name,address,lat,lon) VALUES(?,?,?,?,?)",
                          (session["uid"], request.form.get("loc_name", "").strip(), g_[2], g_[0], g_[1]))
            else:
                flash("We could not find that address. Try adding the city and country.")
        elif act == "del_loc":
            d.execute("DELETE FROM locations WHERE id=? AND user_id=?", (request.form["id"], session["uid"]))
        d.commit(); return redirect(url_for("profile"))
    locs = d.execute("SELECT * FROM locations WHERE user_id=?", (session["uid"],)).fetchall()
    u = me()
    return render_template("profile.html", locs=locs, have=json.loads(u["have"]), seek=json.loads(u["seek"]), my_langs=json.loads(u["languages"] or "[]"))

# ---------- library ----------
@app.route("/library")
@login_required
def library():
    books = db().execute("SELECT * FROM books WHERE user_id=? ORDER BY title", (session["uid"],)).fetchall()
    return render_template("library.html", books=books)

@app.route("/library/new", methods=["GET", "POST"])
@app.route("/library/<int:bid>/edit", methods=["GET", "POST"])
@login_required
def book_form(bid=None):
    d = db()
    locs = d.execute("SELECT * FROM locations WHERE user_id=?", (session["uid"],)).fetchall()
    book = d.execute("SELECT * FROM books WHERE id=? AND user_id=?", (bid, session["uid"])).fetchone() if bid else None
    if request.method == "POST":
        f = request.form
        title, lang, author = f.get("title", "").strip(), f.get("language", ""), f.get("author", "").strip()
        fill, blank = f.get("fill_mode", "manual"), bool(f.get("author_blank"))
        if blank:
            author = ""
        elif fill == "auto" and not author:
            author = (ol_search(title, 1, "title") or [{}])[0].get("author", "")
        if not blank and not author:
            flash("Enter an author, or tick “Leave author blank”.")
        elif not title or not lang:
            flash("Title and language are required.")
        elif not locs:
            flash("Add at least one location in your profile before listing a book.")
        else:
            cover = save_photo(request.files.get("cover")) or f.get("cover_url") or (book["cover"] if book else None)
            vals = (title, author, lang, f.get("year", ""), f.get("info", ""), cover,
                    f.get("theme", ""), ",".join(f.getlist("methods")) or "Borrow", f.get("location_id"))
            if book:
                d.execute("UPDATE books SET title=?,author=?,language=?,year=?,info=?,cover=?,theme=?,methods=?,location_id=? WHERE id=?", vals + (bid,))
            else:
                d.execute("INSERT INTO books(title,author,language,year,info,cover,theme,methods,location_id,user_id) VALUES(?,?,?,?,?,?,?,?,?,?)", vals + (session["uid"],))
            d.commit(); return redirect(url_for("library"))
    return render_template("book_form.html", book=book, locs=locs)

@app.route("/library/<int:bid>/delete", methods=["POST"])
@login_required
def book_delete(bid):
    db().execute("DELETE FROM books WHERE id=? AND user_id=?", (bid, session["uid"])); db().commit()
    return redirect(url_for("library"))

# ---------- search ----------
def lev(a, b, cap=3):
    """Levenshtein distance, exits early once above cap."""
    if abs(len(a) - len(b)) > cap: return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]

def variants(q, kind):
    """Names/titles in other languages, from Open Library (best effort)."""
    out = set()
    for r in ol_search(q, 5):
        out.add(r["title" if kind == "title" else "author"].lower())
    return {v for v in out if v}

def text_match(q, value, similar, extra=()):
    q, v = q.lower().strip(), (value or "").lower()
    if not q: return True
    if q in v: return True
    if not similar: return False
    words = [w for w in q.split() if len(w) > 2]
    if any(w in v for w in words): return True                      # contains searched words
    if lev(q, v) <= 3: return True                                  # up to 3 characters different
    if any(lev(w, vw) <= 3 for w in words for vw in v.split()): return True
    return any(x in v or v in x for x in extra if x)               # other-language names

@app.route("/search")
@login_required
def search():
    d, a = db(), request.args
    locs = d.execute("SELECT * FROM locations WHERE user_id=?", (session["uid"],)).fetchall()
    results, error, searched = [], None, bool(a)
    if searched:
        t, au = a.get("title", "").strip(), a.get("author", "").strip()
        sim_t, sim_a = bool(a.get("sim_title")), bool(a.get("sim_author"))
        methods = a.getlist("methods")
        origin, maxkm = None, None
        if a.get("use_distance"):
            maxkm = float(a.get("km", 25))
            if a.get("loc") and a["loc"] != "new":
                r = d.execute("SELECT lat,lon FROM locations WHERE id=? AND user_id=?", (a["loc"], session["uid"])).fetchone()
                origin = (r["lat"], r["lon"]) if r else None
            elif a.get("new_loc"):
                g_ = geocode(a["new_loc"]); origin = g_[:2] if g_ else None
            if not origin: error = "Distance search needs a location. Pick one of yours or type an address."
        if not error:
            tv = variants(t, "title") if sim_t and t else set()
            av = variants(au, "author") if sim_a and au else set()
            rows = d.execute("""SELECT b.*, u.name AS owner, u.id AS owner_id, u.photo AS owner_photo,
                l.lat, l.lon FROM books b JOIN users u ON u.id=b.user_id
                LEFT JOIN locations l ON l.id=b.location_id WHERE b.user_id!=?""", (session["uid"],)).fetchall()
            for b in rows:
                if not text_match(t, b["title"], sim_t, tv) or not text_match(au, b["author"], sim_a, av): continue
                if methods and not set(methods) & set(b["methods"].split(",")): continue
                km = None
                if origin and b["lat"] is not None:
                    km = haversine(*origin, b["lat"], b["lon"])
                    if km > maxkm: continue
                results.append((b, km))
            results.sort(key=lambda r: (r[1] is None, r[1] or 0))
    return render_template("search.html", results=results, locs=locs, error=error, a=a, searched=searched)

# ---------- suggestions ----------
@app.route("/suggestions")
@login_required
def suggestions():
    d = db()
    mine = d.execute("SELECT * FROM books WHERE user_id=?", (session["uid"],)).fetchall()
    u = me()
    themes = {b["theme"] for b in mine if b["theme"]} | set(json.loads(u["have"])) | set(json.loads(u["seek"]))
    authors = {b["author"] for b in mine if b["author"] and b["author"] != "Unknown"}
    have_titles = {b["title"].lower() for b in mine}
    langs = json.loads(u["languages"] or "[]")       # empty = no language filter
    allowed = {LANG_CODES[l] for l in langs if l in LANG_CODES}
    def lang_ok(r):                                  # Open Library results
        codes = set(r["languages"])
        if not langs: return True
        return bool(codes & allowed) or ("Other" in langs and bool(codes - set(LANG_CODES.values())))
    community = []
    for b in d.execute("SELECT b.*, u.name AS owner FROM books b JOIN users u ON u.id=b.user_id WHERE b.user_id!=?", (session["uid"],)):
        if (b["theme"] in themes or b["author"] in authors) and (not langs or b["language"] in langs):
            community.append(b)
    web = []
    for th in list(themes)[:3]:
        for r in ol_search(f"subject:{th.lower()}", 40 if langs else 6):
            if r["title"].lower() not in have_titles and lang_ok(r): web.append(r)
    for au in list(authors)[:2]:
        for r in ol_search(f"author:{au}", 30 if langs else 4):
            if r["title"].lower() not in have_titles and lang_ok(r): web.append(r)
    return render_template("suggestions.html", community=community[:12], web=web[:18], has_data=bool(themes or authors), langs=langs)

# ---------- map ----------
@app.route("/map")
@login_required
def map_page():
    locs = db().execute("SELECT * FROM locations WHERE user_id=?", (session["uid"],)).fetchall()
    return render_template("map.html", locs=[dict(id=l["id"], name=l["name"] or l["address"], lat=l["lat"], lon=l["lon"]) for l in locs])

# ---------- messages ----------
def conversations():
    m = session["uid"]
    return [dict(r) for r in db().execute("""SELECT u.id, u.name, u.photo, MAX(m.ts) AS last,
        SUM(CASE WHEN m.recipient=? AND m.is_read=0 THEN 1 ELSE 0 END) AS unread FROM messages m
        JOIN users u ON u.id = CASE WHEN m.sender=? THEN m.recipient ELSE m.sender END
        WHERE m.sender=? OR m.recipient=? GROUP BY u.id ORDER BY last DESC""", (m, m, m, m))]

@app.route("/messages")
@login_required
def messages():
    return render_template("messages.html", people=conversations(), other=None, thread=[])

@app.route("/messages/<int:uid>", methods=["GET", "POST"])
@login_required
def thread(uid):
    d = db()
    other = d.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not other or uid == session["uid"]: return redirect(url_for("messages"))
    if request.method == "POST" and request.form.get("body", "").strip():
        d.execute("INSERT INTO messages(sender,recipient,body,ts,is_read) VALUES(?,?,?,?,0)",
                  (session["uid"], uid, request.form["body"].strip(), time.time())); d.commit()
        return redirect(url_for("thread", uid=uid))
    d.execute("UPDATE messages SET is_read=1 WHERE sender=? AND recipient=? AND is_read=0", (uid, session["uid"]))
    d.commit()                                      # opening the conversation marks it as read
    msgs = d.execute("""SELECT * FROM messages WHERE (sender=? AND recipient=?) OR (sender=? AND recipient=?)
        ORDER BY ts""", (session["uid"], uid, uid, session["uid"])).fetchall()
    people = conversations()
    if uid not in [p["id"] for p in people]:
        people.insert(0, dict(id=other["id"], name=other["name"], photo=other["photo"], last=None, unread=0))
    loans = {l["id"]: l for l in d.execute("SELECT * FROM loans WHERE (lender_id=? AND borrower_id=?) OR (lender_id=? AND borrower_id=?)",
                                            (session["uid"], uid, uid, session["uid"]))}
    def lendable(owner):
        return d.execute("SELECT id,title,author FROM books WHERE user_id=? AND (','||methods||',') LIKE '%,Borrow,%' ORDER BY title", (owner,)).fetchall()
    return render_template("messages.html", people=people, other=other, thread=msgs, loans=loans,
                           mine=lendable(session["uid"]), theirs=lendable(uid), today=date.today().isoformat())

# ---------- loans (agreed borrow dates) ----------
@app.template_filter("nice_date")
def nice_date(s):
    return date.fromisoformat(s).strftime("%a %d %b %Y") if s else ""

def notify(to, body, loan_id=None):
    db().execute("INSERT INTO messages(sender,recipient,body,ts,is_read,loan_id) VALUES(?,?,?,?,0,?)",
                 (session["uid"], to, body, time.time(), loan_id))

@app.route("/messages/<int:uid>/propose", methods=["POST"])
@login_required
def propose(uid):
    d, me_id, f = db(), session["uid"], request.form
    back = redirect(url_for("thread", uid=uid))
    b = d.execute("SELECT * FROM books WHERE id=?", (f.get("book_id"),)).fetchone()
    try:
        due = date.fromisoformat(f.get("due_date", ""))
        start = date.fromisoformat(f["start_date"]) if f.get("start_date") else None
    except ValueError:
        flash("Please choose a return date."); return back
    if not d.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone() or uid == me_id: return redirect(url_for("messages"))
    if not b or b["user_id"] not in (me_id, uid) or "Borrow" not in b["methods"].split(","):
        flash("Choose one of the books that can be borrowed."); return back
    if due < date.today() or (start and start > due):
        flash("The return date can't be in the past or before the pick-up date."); return back
    lender = b["user_id"]; borrower = uid if lender == me_id else me_id
    cur = d.execute("INSERT INTO loans(book_id,title,lender_id,borrower_id,start_date,due_date,status,proposed_by,created) VALUES(?,?,?,?,?,?,?,?,?)",
                    (b["id"], b["title"], lender, borrower, start.isoformat() if start else None, due.isoformat(), "proposed", me_id, time.time()))
    notify(uid, f"Proposed borrow dates for “{b['title']}”", cur.lastrowid)
    d.commit(); return back

@app.route("/loans/<int:lid>/<action>", methods=["POST"])
@login_required
def loan_action(lid, action):
    d, me_id = db(), session["uid"]
    l = d.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone()
    if not l or me_id not in (l["lender_id"], l["borrower_id"]): abort(404)
    other = l["borrower_id"] if me_id == l["lender_id"] else l["lender_id"]
    st, t = l["status"], l["title"]
    if action in ("accept", "decline") and st == "proposed" and l["proposed_by"] != me_id:
        new, text = ("accepted", f"✅ Agreed on the borrow dates for “{t}”.") if action == "accept" else ("declined", f"Declined the borrow dates for “{t}”.")
    elif action == "cancel" and st == "proposed" and l["proposed_by"] == me_id:
        new, text = "cancelled", f"Withdrew the borrow proposal for “{t}”."
    elif action == "returned" and st == "accepted" and me_id == l["lender_id"]:
        new, text = "returned", f"📚 “{t}” has been returned. Thank you!"
    else:
        flash("That action isn't available."); return redirect(url_for("thread", uid=other))
    d.execute("UPDATE loans SET status=? WHERE id=?", (new, lid)); notify(other, text); d.commit()
    return redirect(url_for("calendar_page") if action == "returned" else url_for("thread", uid=other))

# ---------- calendar ----------
def abs_url(path):
    return (os.environ.get("BASE_URL") or request.host_url).rstrip("/") + path

def my_loans(uid, only=None):
    rows = db().execute("""SELECT l.*, lu.name AS lender_name, bu.name AS borrower_name FROM loans l
        JOIN users lu ON lu.id=l.lender_id JOIN users bu ON bu.id=l.borrower_id
        WHERE (l.lender_id=? OR l.borrower_id=?) AND l.status='accepted' ORDER BY l.due_date""", (uid, uid)).fetchall()
    out = []
    for r in rows:
        if only and r["id"] != only: continue
        l, lending = dict(r), r["lender_id"] == uid
        l.update(me=uid, role="lending" if lending else "borrowing",
                 other_id=r["borrower_id"] if lending else r["lender_id"],
                 other_name=((r["borrower_name"] if lending else r["lender_name"]) or "").strip() or "Anonymous")
        out.append(l)
    return out

def gcal_link(e):
    d = date.fromisoformat(e["date"])
    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(
        {"action": "TEMPLATE", "text": e["title"], "dates": f"{d:%Y%m%d}/{d + timedelta(days=1):%Y%m%d}", "details": e["desc"]})

def loan_events(l):
    lending, name, t = l["role"] == "lending", l["other_name"], l["title"]
    chat = abs_url(url_for("thread", uid=l["other_id"]))
    desc = f"Book: {t}\n{'Lending to' if lending else 'Borrowed from'}: {name}\nChat: {chat}"
    ev = []
    if l["start_date"]:
        ev.append(dict(kind="pickup", date=l["start_date"], chat=chat, desc=desc,
                       title=f"Hand over: {t} (to {name})" if lending else f"Pick up: {t} (from {name})"))
    ev.append(dict(kind="return", date=l["due_date"], chat=chat, desc=desc,
                   title=f"Get back: {t} (from {name})" if lending else f"Return: {t} (to {name})"))
    for e in ev: e["google"] = gcal_link(e)
    return ev

def esc(x): return x.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

def fold(line):
    parts, cur, n = [], "", 0
    for ch in line:
        b = len(ch.encode())
        if n + b > 73: parts.append(cur); cur, n = " ", 1
        cur += ch; n += b
    return "\r\n".join(parts + [cur])

def make_ics(loans):
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Community Library//EN", "CALSCALE:GREGORIAN", "X-WR-CALNAME:Community Library"]
    for l in loans:
        for e in loan_events(l):
            d = date.fromisoformat(e["date"])
            L += ["BEGIN:VEVENT", f"UID:loan{l['id']}-{e['kind']}-u{l['me']}@communitylibrary", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}",
                  "SUMMARY:" + esc(e["title"]), "DESCRIPTION:" + esc(e["desc"]), "URL:" + e["chat"]]
            if e["kind"] == "return":
                L += ["BEGIN:VALARM", "TRIGGER:-P1D", "ACTION:DISPLAY", "DESCRIPTION:" + esc(e["title"]), "END:VALARM"]
            L.append("END:VEVENT")
    L.append("END:VCALENDAR")
    return "\r\n".join(fold(x) for x in L) + "\r\n"

def ics_response(text, name=None):
    r = Response(text, mimetype="text/calendar")
    if name: r.headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return r

@app.route("/calendar")
@login_required
def calendar_page():
    d, today = db(), date.today()
    try:
        y, m = map(int, request.args.get("m", "").split("-")); date(y, m, 1)
    except ValueError:
        y, m = today.year, today.month
    loans = my_loans(session["uid"])
    events = {}
    for l in loans:
        l["events"] = loan_events(l)
        for e in l["events"]: events.setdefault(e["date"], []).append(dict(e, loan=l))
    first = date(y, m, 1)
    row = d.execute("SELECT cal_token FROM users WHERE id=?", (session["uid"],)).fetchone()
    token = row["cal_token"]
    if not token:
        token = secrets.token_urlsafe(24)
        d.execute("UPDATE users SET cal_token=? WHERE id=?", (token, session["uid"])); d.commit()
    return render_template("calendar.html", weeks=pycal.Calendar(0).monthdatescalendar(y, m), y=y, m=m,
        month_name=pycal.month_name[m], events=events, loans=loans, today=today, today_iso=today.isoformat(),
        prev=f"{(first - timedelta(days=1)):%Y-%m}", nxt=f"{(first + timedelta(days=32)):%Y-%m}",
        feed_url=abs_url(url_for("calendar_feed", token=token)),
        is_local=request.host.split(":")[0] in ("127.0.0.1", "localhost"))

@app.route("/calendar/all.ics")
@login_required
def calendar_all():
    return ics_response(make_ics(my_loans(session["uid"])), "community-library.ics")

@app.route("/calendar/loan/<int:lid>.ics")
@login_required
def calendar_loan(lid):
    loans = my_loans(session["uid"], only=lid)
    if not loans: abort(404)
    return ics_response(make_ics(loans), f"borrow-{lid}.ics")

@app.route("/calendar/feed/<token>.ics")
def calendar_feed(token):
    u = db().execute("SELECT id FROM users WHERE cal_token=?", (token,)).fetchone()
    if not u: abort(404)
    return ics_response(make_ics(my_loans(u["id"])))

@app.route("/calendar/new-link", methods=["POST"])
@login_required
def calendar_new_link():
    db().execute("UPDATE users SET cal_token=? WHERE id=?", (secrets.token_urlsafe(24), session["uid"])); db().commit()
    flash("New private calendar link created. Re-subscribe in Google Calendar; the old link no longer works.")
    return redirect(url_for("calendar_page"))

if __name__ == "__main__":
    os.makedirs(UPLOADS, exist_ok=True)
    init_db()
    app.run(debug=True)

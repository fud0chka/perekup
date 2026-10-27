# -*- coding: utf-8 -*-
"""
Паста перекупа: сервер аккаунтов + облако сейвов + промокоды + дев-консоль.
Запуск:  python server.py            (порт 8900)
База:    perekup.db рядом с файлом. Админ: ник opiatium (без @, регистр не важен).

Аккаунт = ник (+ уникальный id). Полный сейв игры хранится на сервере,
привязан к аккаунту. Удаление аккаунта чистит прогресс.
API (все JSON):
  POST /api/register {nick}                -> {ok, id, nick, token, is_admin}
  POST /api/login    {nick}                -> {ok, id, nick, token, is_admin}
  GET  /api/state?token=                   -> {ok, id, nick, is_admin, save, luck}
  POST /api/save    {token, save}          -> {ok}
  POST /api/account/delete {token}         -> {ok}   (свой аккаунт + прогресс)
  GET  /api/meta?token=                    -> {ok, luck}
  GET  /api/promos?token=                  -> {ok, list:[{code,money,model,gbIdx,cond,target,uses_left}]}
  POST /api/redeem  {token, code}          -> {ok}   (учёт использования)
  GET  /api/grants?token=                  -> {ok, list:[{id, kind, payload}]}
  POST /api/grants/ack {token, ids:[..]}   -> {ok}
  GET  /api/dev/accounts?token=            -> {ok, list:[{id,nick,is_admin,luck}]}
  POST /api/dev/give   {token, target?, money}      -> выдать ₽ (target пусто/нет = себе)
  POST /api/dev/rep    {token, target?, delta}      -> репутация ±
  POST /api/dev/phone  {token, target?, model, gbIdx, cond}
  POST /api/dev/chance {token, target?, factor}     -> 0.25..3 (target пусто = глобально)
  POST /api/dev/promo  {token, code, money, model?, gbIdx?, cond?, uses?, target?}
  POST /api/dev/delete {token, nick}                -> удалить чужой аккаунт
"""
import io
import json
import os
import secrets
import sqlite3
import time

from flask import Flask, request, jsonify, Response

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "perekup.db")
GAME_HTML = os.path.join(BASE_DIR, "Перекуп — игра про перепродажу телефонов.html")

ADMIN_NICK = "opiatium"
MIN_LUCK, MAX_LUCK = 0.25, 3.0

app = Flask(__name__, static_folder=None)


# ---------------- база ----------------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              nick TEXT UNIQUE COLLATE NOCASE NOT NULL,
              token TEXT UNIQUE,
              is_admin INTEGER DEFAULT 0,
              save TEXT,
              luck REAL,
              created_at INTEGER,
              updated_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS promos(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              code TEXT UNIQUE COLLATE NOCASE NOT NULL,
              money REAL DEFAULT 0,
              model TEXT DEFAULT '',
              gb_idx INTEGER DEFAULT 0,
              cond INTEGER DEFAULT 0,
              uses_max INTEGER DEFAULT 0,
              used_by TEXT DEFAULT '[]',
              target TEXT DEFAULT '',
              active INTEGER DEFAULT 1,
              created_by TEXT DEFAULT '',
              created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS grants(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              account_id INTEGER NOT NULL,
              kind TEXT NOT NULL,
              payload TEXT NOT NULL,
              acked INTEGER DEFAULT 0,
              created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            """
        )
        # глобальный шанс
        con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('luck_global','1')")


def now():
    return int(time.time() * 1000)


def acc_by_token(con, token):
    if not token:
        return None
    return con.execute(
        "SELECT * FROM accounts WHERE token=?", (token,)
    ).fetchone()


def grant_add(con, account_id, kind, payload):
    con.execute(
        "INSERT INTO grants(account_id, kind, payload, created_at) VALUES(?,?,?,?)",
        (account_id, kind, json.dumps(payload, ensure_ascii=False), now()),
    )


# ---------------- статика ----------------
@app.route("/")
def index():
    try:
        with io.open(GAME_HTML, "r", encoding="utf-8") as f:
            html = f.read()
        return Response(html, mimetype="text/html")
    except OSError:
        return "game html not found", 404


# ---------------- аккаунты ----------------
@app.route("/api/register", methods=["POST"])
def register():
    d = request.get_json(silent=True) or {}
    nick = str(d.get("nick", "")).strip()
    if len(nick) < 2 or len(nick) > 24:
        return jsonify(ok=0, err="Ник: от 2 до 24 символов")
    with db() as con:
        if con.execute("SELECT 1 FROM accounts WHERE nick=?", (nick,)).fetchone():
            return jsonify(ok=0, err="Этот ник уже занят")
        token = secrets.token_hex(16)
        cur = con.execute(
            "INSERT INTO accounts(nick, token, is_admin, created_at, updated_at) VALUES(?,?,?,?,?)",
            (nick, token, 1 if nick.lower() == ADMIN_NICK else 0, now(), now()),
        )
        aid = cur.lastrowid
        admin = 1 if nick.lower() == ADMIN_NICK else 0
    return jsonify(ok=1, id=aid, nick=nick, token=token, is_admin=admin)


@app.route("/api/login", methods=["POST"])
def login():
    d = request.get_json(silent=True) or {}
    nick = str(d.get("nick", "")).strip()
    with db() as con:
        a = con.execute("SELECT * FROM accounts WHERE nick=?", (nick,)).fetchone()
        if not a:
            return jsonify(ok=0, err="Аккаунт не найден")
        return jsonify(
            ok=1, id=a["id"], nick=a["nick"], token=a["token"], is_admin=a["is_admin"]
        )


@app.route("/api/state")
def state():
    token = request.args.get("token", "")
    with db() as con:
        a = acc_by_token(con, token)
        if not a:
            return jsonify(ok=0, err="Нужен вход"), 401
        luck = global_luck(con)
        return jsonify(
            ok=1,
            id=a["id"],
            nick=a["nick"],
            is_admin=a["is_admin"],
            save=a["save"],
            luck=a["luck"] if a["luck"] is not None else luck,
        )


@app.route("/api/save", methods=["POST"])
def save_state():
    d = request.get_json(silent=True) or {}
    with db() as con:
        a = acc_by_token(con, d.get("token", ""))
        if not a:
            return jsonify(ok=0, err="Нужен вход"), 401
        raw = str(d.get("save", ""))[:6_000_000]
        if not raw:
            return jsonify(ok=0, err="Пустой сейв")
        con.execute(
            "UPDATE accounts SET save=?, updated_at=? WHERE id=?", (raw, now(), a["id"])
        )
    return jsonify(ok=1)


@app.route("/api/account/delete", methods=["POST"])
def delete_self():
    d = request.get_json(silent=True) or {}
    with db() as con:
        a = acc_by_token(con, d.get("token", ""))
        if not a:
            return jsonify(ok=0, err="Нужен вход"), 401
        con.execute("DELETE FROM accounts WHERE id=?", (a["id"],))
        con.execute("DELETE FROM grants WHERE account_id=?", (a["id"],))
    return jsonify(ok=1)


@app.route("/api/meta")
def meta():
    token = request.args.get("token", "")
    with db() as con:
        a = acc_by_token(con, token)
        luck = global_luck(con)
        if a and a["luck"] is not None:
            luck = a["luck"]
    return jsonify(ok=1, luck=luck)


def global_luck(con):
    r = con.execute("SELECT value FROM meta WHERE key='luck_global'").fetchone()
    try:
        return float(r["value"]) if r else 1.0
    except (TypeError, ValueError):
        return 1.0


# ---------------- промокоды ----------------
@app.route("/api/promos")
def promos():
    token = request.args.get("token", "")
    with db() as con:
        a = acc_by_token(con, token)
        if not a:
            return jsonify(ok=0, err="Нужен вход"), 401
        rows = con.execute(
            "SELECT * FROM promos WHERE active=1 ORDER BY id DESC"
        ).fetchall()
        out = []
        for r in rows:
            used = json.loads(r["used_by"] or "[]")
            if a["id"] in used:
                continue
            uses_left = -1 if not r["uses_max"] else max(0, r["uses_max"] - len(used))
            if r["uses_max"] and uses_left == 0:
                continue
            out.append(
                dict(
                    code=r["code"],
                    money=r["money"],
                    model=r["model"],
                    gbIdx=r["gb_idx"],
                    cond=r["cond"],
                    target=r["target"],
                    usesLeft=uses_left,
                )
            )
    return jsonify(ok=1, list=out)


@app.route("/api/redeem", methods=["POST"])
def redeem():
    d = request.get_json(silent=True) or {}
    code = str(d.get("code", "")).strip()
    with db() as con:
        a = acc_by_token(con, d.get("token", ""))
        if not a:
            return jsonify(ok=0, err="Нужен вход"), 401
        r = con.execute("SELECT * FROM promos WHERE code=? AND active=1", (code,)).fetchone()
        if not r:
            return jsonify(ok=0)
        used = json.loads(r["used_by"] or "[]")
        if a["id"] not in used:
            used.append(a["id"])
            con.execute(
                "UPDATE promos SET used_by=? WHERE id=?", (json.dumps(used), r["id"])
            )
    return jsonify(ok=1)


# ---------------- гранты ----------------
@app.route("/api/grants")
def grants():
    token = request.args.get("token", "")
    with db() as con:
        a = acc_by_token(con, token)
        if not a:
            return jsonify(ok=0, err="Нужен вход"), 401
        rows = con.execute(
            "SELECT id, kind, payload FROM grants WHERE account_id=? AND acked=0 ORDER BY id",
            (a["id"],),
        ).fetchall()
        out = [dict(id=r["id"], kind=r["kind"], payload=json.loads(r["payload"])) for r in rows]
    return jsonify(ok=1, list=out)


@app.route("/api/grants/ack", methods=["POST"])
def grants_ack():
    d = request.get_json(silent=True) or {}
    ids = d.get("ids") or []
    if not isinstance(ids, list):
        return jsonify(ok=0)
    qs = ",".join("?" for _ in ids)
    with db() as con:
        a = acc_by_token(con, d.get("token", ""))
        if not a:
            return jsonify(ok=0, err="Нужен вход"), 401
        if ids:
            con.execute(
                "UPDATE grants SET acked=1 WHERE account_id=? AND id IN (%s)" % qs,
                [a["id"]] + [int(i) for i in ids if str(i).lstrip("-").isdigit()],
            )
    return jsonify(ok=1)


# ---------------- дев-консоль ----------------
def need_admin(con, token):
    a = acc_by_token(con, token)
    if not a or not a["is_admin"]:
        return None
    return a


@app.route("/api/dev/accounts")
def dev_accounts():
    with db() as con:
        if not need_admin(con, request.args.get("token", "")):
            return jsonify(ok=0, err="Только для админа"), 403
        rows = con.execute(
            "SELECT id, nick, is_admin, luck FROM accounts ORDER BY id"
        ).fetchall()
        return jsonify(
            ok=1,
            list=[
                dict(id=r["id"], nick=r["nick"], is_admin=r["is_admin"], luck=r["luck"])
                for r in rows
            ],
        )


def dev_target(con, me, nick):
    """None -> свой аккаунт; иначе строка ник или id."""
    if not nick:
        return me
    row = con.execute("SELECT * FROM accounts WHERE nick=?", (str(nick),)).fetchone()
    if not row and str(nick).isdigit():
        row = con.execute(
            "SELECT * FROM accounts WHERE id=?", (int(nick),)
        ).fetchone()
    if not row:
        return None
    return row


@app.route("/api/dev/give", methods=["POST"])
def dev_give():
    d = request.get_json(silent=True) or {}
    money = float(d.get("money") or 0)
    with db() as con:
        me = need_admin(con, d.get("token", ""))
        if not me:
            return jsonify(ok=0, err="Только для админа"), 403
        t = dev_target(con, me, d.get("target", ""))
        if t is None:
            return jsonify(ok=0, err="Аккаунт не найден")
        if not money:
            return jsonify(ok=0, err="Сумма = 0")
        grant_add(con, t["id"], "money", {"money": round(money, 2)})
        who = "себе" if t["id"] == me["id"] else t["nick"]
    return jsonify(ok=1, msg="Выдано %s ₽ (%s)" % (money, who))


@app.route("/api/dev/rep", methods=["POST"])
def dev_rep():
    d = request.get_json(silent=True) or {}
    delta = float(d.get("delta") or 0)
    with db() as con:
        me = need_admin(con, d.get("token", ""))
        if not me:
            return jsonify(ok=0, err="Только для админа"), 403
        t = dev_target(con, me, d.get("target", ""))
        if t is None:
            return jsonify(ok=0, err="Аккаунт не найден")
        if not delta:
            return jsonify(ok=0, err="Дельта = 0")
        grant_add(con, t["id"], "rep", {"delta": delta})
    return jsonify(ok=1)


@app.route("/api/dev/phone", methods=["POST"])
def dev_phone():
    d = request.get_json(silent=True) or {}
    model = str(d.get("model", "")).strip()
    with db() as con:
        me = need_admin(con, d.get("token", ""))
        if not me:
            return jsonify(ok=0, err="Только для админа"), 403
        t = dev_target(con, me, d.get("target", ""))
        if t is None:
            return jsonify(ok=0, err="Аккаунт не найден")
        if not model:
            return jsonify(ok=0, err="Нужна модель")
        grant_add(
            con,
            t["id"],
            "phone",
            {
                "model": model,
                "gbIdx": int(d.get("gbIdx") or 0),
                "cond": int(d.get("cond") or 0),
            },
        )
    return jsonify(ok=1)


@app.route("/api/dev/chance", methods=["POST"])
def dev_chance():
    d = request.get_json(silent=True) or {}
    factor = float(d.get("factor") or 1)
    factor = max(MIN_LUCK, min(MAX_LUCK, factor))
    with db() as con:
        me = need_admin(con, d.get("token", ""))
        if not me:
            return jsonify(ok=0, err="Только для админа"), 403
        if d.get("global"):
            con.execute(
                "UPDATE meta SET value=? WHERE key='luck_global'", (str(factor),)
            )
            return jsonify(ok=1, msg="Глобальный шанс ×%s" % factor)
        t = dev_target(con, me, d.get("target", ""))
        if t is None:
            return jsonify(ok=0, err="Аккаунт не найден")
        con.execute("UPDATE accounts SET luck=? WHERE id=?", (factor, t["id"]))
        return jsonify(ok=1, msg="Шанс ×%s для %s" % (factor, t["nick"]))


@app.route("/api/dev/promo", methods=["POST"])
def dev_promo():
    d = request.get_json(silent=True) or {}
    code = str(d.get("code", "")).strip().upper().replace(" ", "")
    if len(code) < 3:
        return jsonify(ok=0, err="Код: минимум 3 символа")
    with db() as con:
        me = need_admin(con, d.get("token", ""))
        if not me:
            return jsonify(ok=0, err="Только для админа"), 403
        if con.execute("SELECT 1 FROM promos WHERE code=?", (code,)).fetchone():
            return jsonify(ok=0, err="Такой код уже есть")
        target = str(d.get("target", "") or "").strip()
        if target:
            t = dev_target(con, me, target)
            if t is None:
                return jsonify(ok=0, err="Аккаунт не найден")
            target = t["nick"]
        con.execute(
            "INSERT INTO promos(code, money, model, gb_idx, cond, uses_max, target, created_by, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                code,
                float(d.get("money") or 0),
                str(d.get("model", "") or ""),
                int(d.get("gbIdx") or 0),
                int(d.get("cond") or 0),
                int(d.get("uses") or 0),
                target,
                me["nick"],
                now(),
            ),
        )
        # если привязан к игроку — грант: игрок активирует автоматически
        if target:
            grant_add(
                con,
                con.execute("SELECT id FROM accounts WHERE nick=?", (target,)).fetchone()["id"],
                "promo",
                dict(
                    code=code,
                    money=float(d.get("money") or 0),
                    model=str(d.get("model", "") or ""),
                    gbIdx=int(d.get("gbIdx") or 0),
                    cond=int(d.get("cond") or 0),
                ),
            )
    return jsonify(ok=1, msg="Промокод %s создан" % code)


@app.route("/api/dev/delete", methods=["POST"])
def dev_delete():
    d = request.get_json(silent=True) or {}
    nick = str(d.get("nick", "")).strip()
    with db() as con:
        me = need_admin(con, d.get("token", ""))
        if not me:
            return jsonify(ok=0, err="Только для админа"), 403
        t = con.execute("SELECT * FROM accounts WHERE nick=? OR id=?", (nick, int(nick) if nick.isdigit() else -1)).fetchone()
        if not t:
            return jsonify(ok=0, err="Аккаунт не найден")
        con.execute("DELETE FROM accounts WHERE id=?", (t["id"],))
        con.execute("DELETE FROM grants WHERE account_id=?", (t["id"],))
    return jsonify(ok=1, msg="Аккаунт %s удалён" % t["nick"])


init_db()

from server import app

application = app  # единое имя для WSGI-серверов

if __name__ == "__main__":
    print("Паста перекупа: сервер на http://127.0.0.1:8900")
    app.run(host="127.0.0.1", port=8900, threaded=True)

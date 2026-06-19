import os
import redis
import uvicorn
import secrets
import bcrypt
import hashlib
import json
import threading
import time

from bson import ObjectId
from pymongo import MongoClient, ASCENDING
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from cassandra.cluster import Cluster
from cassandra.auth import PlainTextAuthProvider
from cassandra.query import dict_factory
from cassandra import ConsistencyLevel


app = FastAPI()

redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST"),
    port=int(os.getenv("REDIS_PORT")),
    password=os.getenv("REDIS_PASSWORD") or None,
    db=int(os.getenv("REDIS_DB")),
    decode_responses=True
)

mongo_client = MongoClient(
    f"mongodb://{os.getenv('MONGODB_HOST')}:{os.getenv('MONGODB_PORT')}"
)

db = mongo_client[os.getenv("MONGODB_DATABASE")]

users_collection = db["users"]
events_collection = db["events"]

users_collection.create_index("username", unique=True)
events_collection.create_index([("title", ASCENDING)])
events_collection.create_index([("title", ASCENDING), ("created_by", ASCENDING)])
events_collection.create_index([("created_by", ASCENDING)])

SESSION_COOKIE_NAME = "X-Session-Id"

cass_session = None
cass_lock = None

def get_cassandra():
    global cass_session, cass_lock
    if cass_session is not None:
        return cass_session
    if cass_lock is None:
        import threading
        cass_lock = threading.Lock()
    with cass_lock:
        if cass_session is not None:
            return cass_session
        hosts = os.getenv("CASSANDRA_HOSTS", "cassandra").split(",")
        port = int(os.getenv("CASSANDRA_PORT", "9042"))
        keyspace = os.getenv("CASSANDRA_KEYSPACE", "testkeyspace")
        for attempt in range(5):
            try:
                if os.getenv("CASSANDRA_USERNAME") and os.getenv("CASSANDRA_PASSWORD"):
                    from cassandra.auth import PlainTextAuthProvider
                    auth_provider = PlainTextAuthProvider(
                        username=os.getenv("CASSANDRA_USERNAME"),
                        password=os.getenv("CASSANDRA_PASSWORD")
                    )
                    cluster = Cluster(hosts, port=port, auth_provider=auth_provider, protocol_version=4)
                else:
                    cluster = Cluster(hosts, port=port, protocol_version=4)
                session = cluster.connect()
                session.execute(f"""
                    CREATE KEYSPACE IF NOT EXISTS {keyspace}
                    WITH replication = {{'class': 'SimpleStrategy', 'replication_factor': 1}}
                """)
                session.set_keyspace(keyspace)
                session.execute("""
                    CREATE TABLE IF NOT EXISTS event_reactions (
                        event_id text,
                        created_by text,
                        like_value tinyint,
                        created_at timestamp,
                        PRIMARY KEY (event_id, created_by)
                    )
                """)
                cl = getattr(ConsistencyLevel, os.getenv("CASSANDRA_CONSISTENCY", "ONE").upper(), ConsistencyLevel.ONE)
                session.default_consistency_level = cl
                session.row_factory = dict_factory
                cass_session = session
                return cass_session
            except Exception as e:
                print(f"Cassandra connection attempt {attempt+1} failed: {e}")
                import time
                time.sleep(2)
        return None

def generate_sid():
    return secrets.token_hex(16)


def get_ttl():
    return int(os.getenv("APP_USER_SESSION_TTL"))


def redis_key(sid: str):
    return f"sid:{sid}"


def now():
    return datetime.now(timezone.utc).isoformat()


def get_session_data(request: Request):
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if not sid:
        return None, None

    key = redis_key(sid)
    data = redis_client.hgetall(key)

    if not data:
        return None, None

    if "user_id" not in data:
        return None, None

    redis_client.expire(key, get_ttl())
    return sid, data


def event_to_response(e, reactions=None):
    location = e.get("location", {})
    result = {
        "id": str(e["_id"]),
        "title": e["title"],
        "category": e.get("category"),
        "price": e.get("price"),
        "description": e.get("description"),
        "location": {
            "city": location.get("city"),
            "address": location.get("address")
        },
        "created_at": e.get("created_at"),
        "created_by": e.get("created_by"),
        "started_at": e.get("started_at"),
        "finished_at": e.get("finished_at"),
    }
    if reactions is not None:
        result["reactions"] = reactions
    return result

def get_title_md5(title: str) -> str:
    return hashlib.md5(title.encode('utf-8')).hexdigest()

def get_reactions_for_title(title: str) -> dict:
    cache_key = f"event:{get_title_md5(title)}:reactions"
    try:
        cached = redis_client.hgetall(cache_key)
        if cached:
            redis_client.expire(cache_key, int(os.getenv("APP_LIKE_TTL", "180")))
            return {"likes": int(cached.get("likes", 0)), "dislikes": int(cached.get("dislikes", 0))}
    except Exception:
        pass

    s = get_cassandra()
    if not s:
        return {"likes": 0, "dislikes": 0}

    event_ids = [str(e["_id"]) for e in events_collection.find({"title": title}, {"_id": 1})]
    likes = 0
    dislikes = 0
    for eid in event_ids:
        rows = s.execute("SELECT like_value FROM event_reactions WHERE event_id=%s", (eid,))
        for row in rows:
            if row["like_value"] == 1:
                likes += 1
            elif row["like_value"] == -1:
                dislikes += 1

    redis_client.hset(cache_key, mapping={"likes": likes, "dislikes": dislikes})
    redis_client.expire(cache_key, int(os.getenv("APP_LIKE_TTL", "180")))
    return {"likes": likes, "dislikes": dislikes}

@app.get("/health")
def health(request: Request, response: Response):
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if sid:
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=sid,
            httponly=True,
            max_age=get_ttl(),
            path="/",
        )
    return {"status": "ok"}


@app.post("/session")
def session(request: Request, response: Response):
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    ttl = get_ttl()

    if not sid:
        for _ in range(5):
            sid = generate_sid()
            key = redis_key(sid)
            if not redis_client.exists(key):
                break

        timestamp = now()
        redis_client.hset(
            key,
            mapping={
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )
        redis_client.expire(key, ttl)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=sid,
            httponly=True,
            max_age=ttl,
            path="/",
        )
        return Response(status_code=201, headers=response.headers)

    key = redis_key(sid)

    if redis_client.exists(key):
        redis_client.hset(key, "updated_at", now())
        redis_client.expire(key, ttl)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=sid,
            httponly=True,
            max_age=ttl,
            path="/",
        )
        return Response(status_code=200, headers=response.headers)

    for _ in range(5):
        sid = generate_sid()
        key = redis_key(sid)
        if not redis_client.exists(key):
            break

    timestamp = now()
    redis_client.hset(
        key,
        mapping={
            "created_at": timestamp,
            "updated_at": timestamp,
        },
    )
    redis_client.expire(key, ttl)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=sid,
        httponly=True,
        max_age=ttl,
        path="/",
    )
    return Response(status_code=201, headers=response.headers)


@app.post("/users")
async def create_user(request: Request, response: Response):
    body = await request.json()

    full_name = body.get("full_name")
    username = body.get("username")
    password = body.get("password")

    if not full_name:
        return JSONResponse(status_code=400, content={"message": 'invalid "full_name" field'})
    if not username:
        return JSONResponse(status_code=400, content={"message": 'invalid "username" field'})
    if not password:
        return JSONResponse(status_code=400, content={"message": 'invalid "password" field'})

    if users_collection.find_one({"username": username}):
        return JSONResponse(status_code=409, content={"message": "user already exists"})

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode("utf-8")

    user = {
        "full_name": full_name,
        "username": username,
        "password_hash": password_hash
    }

    result = users_collection.insert_one(user)

    sid = generate_sid()
    key = redis_key(sid)
    ttl = get_ttl()
    timestamp = now()

    redis_client.hset(
        key,
        mapping={
            "created_at": timestamp,
            "updated_at": timestamp,
            "user_id": str(result.inserted_id),
        },
    )
    redis_client.expire(key, ttl)

    res = Response(status_code=201, content=b"")
    res.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=sid,
        httponly=True,
        max_age=ttl,
        path="/",
    )
    return res


@app.post("/auth/login")
async def login(request: Request, response: Response):
    body = await request.json()

    username = body.get("username")
    password = body.get("password")

    if not username or not password:
        return JSONResponse(status_code=400, content={"message": "invalid credentials"})

    user = users_collection.find_one({"username": username})

    if not user or not bcrypt.checkpw(password.encode(), user["password_hash"].encode("utf-8")):
        return JSONResponse(status_code=401, content={"message": "invalid credentials"})

    ttl = get_ttl()
    sid = request.cookies.get(SESSION_COOKIE_NAME)

    if sid:
        key = redis_key(sid)
        if redis_client.exists(key):
            redis_client.hset(
                key,
                mapping={
                    "updated_at": now(),
                    "user_id": str(user["_id"]),
                },
            )
            redis_client.expire(key, ttl)

            res = Response(status_code=204)
            res.set_cookie(
                key=SESSION_COOKIE_NAME,
                value=sid,
                httponly=True,
                max_age=ttl,
                path="/",
            )
            return res

    for _ in range(5):
        sid = generate_sid()
        key = redis_key(sid)
        if not redis_client.exists(key):
            break

    timestamp = now()
    redis_client.hset(
        key,
        mapping={
            "created_at": timestamp,
            "updated_at": timestamp,
            "user_id": str(user["_id"]),
        }
    )
    redis_client.expire(key, ttl)

    res = Response(status_code=204)
    res.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=sid,
        httponly=True,
        max_age=ttl,
        path="/",
    )
    return res


@app.post("/auth/logout")
def logout(request: Request, response: Response):
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if not sid:
        return Response(status_code=401)

    key = redis_key(sid)
    if not redis_client.exists(key):
        return Response(status_code=401)

    redis_client.delete(key)

    res = Response(status_code=204)
    res.set_cookie(
        key=SESSION_COOKIE_NAME,
        value="",
        httponly=True,
        max_age=0,
        path="/",
    )
    return res


@app.post("/events")
async def create_event(request: Request, response: Response):
    sid, session = get_session_data(request)

    if not session or "user_id" not in session:
        return Response(status_code=401)

    body = await request.json()

    title = body.get("title")
    description = body.get("description")
    address = body.get("address")
    started_at = body.get("started_at")
    finished_at = body.get("finished_at")

    if not title:
        return JSONResponse(status_code=400, content={"message": 'invalid "title" field'})
    if not address:
        return JSONResponse(status_code=400, content={"message": 'invalid "address" field'})
    if not started_at or not isinstance(started_at, str):
        return JSONResponse(status_code=400, content={"message": 'invalid "started_at" field'})
    try:
        datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except Exception:
        return JSONResponse(status_code=400, content={"message": 'invalid "started_at" format'})
    if not finished_at or not isinstance(finished_at, str):
        return JSONResponse(status_code=400, content={"message": 'invalid "finished_at" field'})
    try:
        datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    except Exception:
        return JSONResponse(status_code=400, content={"message": 'invalid "finished_at" format'})

    event = {
        "title": title,
        "description": description,
        "location": {
            "address": address
        },
        "created_at": now(),
        "created_by": session["user_id"],
        "started_at": started_at,
        "finished_at": finished_at,
    }

    result = events_collection.insert_one(event)

    ttl = get_ttl()
    redis_client.expire(redis_key(sid), ttl)

    res = JSONResponse(
        status_code=201,
        content={"id": str(result.inserted_id)}
    )
    res.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=sid,
        httponly=True,
        max_age=ttl,
        path="/",
    )
    return res

def build_events_query(request: Request):
    title = request.query_params.get("title")
    event_id = request.query_params.get("id")
    category = request.query_params.get("category")
    price_from = request.query_params.get("price_from")
    price_to = request.query_params.get("price_to")
    city = request.query_params.get("city")
    date_from = request.query_params.get("date_from")
    date_to = request.query_params.get("date_to")
    user = request.query_params.get("user")

    query = {}

    if title:
        query["title"] = {"$regex": title, "$options": "i"}

    if event_id:
        try:
            query["_id"] = ObjectId(event_id)
        except Exception:
            return None, {"events": [], "count": 0}

    if category:
        valid_categories = ["meetup", "concert", "exhibition", "party", "other"]
        if category not in valid_categories:
            return None, JSONResponse(status_code=400, content={"message": 'invalid "category" field'})
        query["category"] = category

    if price_from is not None or price_to is not None:
        price_filter = {}
        if price_from is not None:
            try:
                p_from = int(price_from)
                price_filter["$gte"] = p_from
            except ValueError:
                return None, JSONResponse(status_code=400, content={"message": 'invalid "price_from" field'})
        if price_to is not None:
            try:
                p_to = int(price_to)
                price_filter["$lte"] = p_to
            except ValueError:
                return None, JSONResponse(status_code=400, content={"message": 'invalid "price_to" field'})
        query["price"] = price_filter

    if city:
        query["location.city"] = city

    if date_from:
        try:
            dt = datetime.strptime(date_from, "%Y%m%d")
            date_from_iso = dt.strftime("%Y-%m-%dT00:00:00Z")
            query["started_at"] = {**query.get("started_at", {}), "$gte": date_from_iso}
        except ValueError:
            return None, JSONResponse(status_code=400, content={"message": 'invalid "date_from" field'})

    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y%m%d")
            date_to_iso = dt.strftime("%Y-%m-%dT23:59:59Z")
            started_at_filter = query.get("started_at", {})
            started_at_filter["$lte"] = date_to_iso
            query["started_at"] = started_at_filter
        except ValueError:
            return None, JSONResponse(status_code=400, content={"message": 'invalid "date_to" field'})

    if user:
        found_user = users_collection.find_one({"username": user})
        if found_user:
            query["created_by"] = str(found_user["_id"])
        else:
            return None, {"events": [], "count": 0}

    return query, None

@app.get("/events")
def get_events(request: Request):
    limit = int(request.query_params.get("limit", 10))
    offset = int(request.query_params.get("offset", 0))

    query_or_error, error_response = build_events_query(request)
    if error_response is not None:
        return error_response

    cursor = events_collection.find(query_or_error).skip(offset).limit(limit)
    include_reactions = request.query_params.get("include") == "reactions"
    events = []
    for e in cursor:
        reactions = get_reactions_for_title(e["title"]) if include_reactions else None
        events.append(event_to_response(e, reactions))

    return {
        "events": events,
        "count": len(events)
    }


@app.get("/events/{event_id}")
def get_event(event_id: str, request: Request):
    try:
        e = events_collection.find_one({"_id": ObjectId(event_id)})
    except Exception:
        e = None

    if e is None:
        return JSONResponse(status_code=404, content={"message": "Not found"})

    include_reactions = request.query_params.get("include") == "reactions"
    reactions = get_reactions_for_title(e["title"]) if include_reactions else None
    return event_to_response(e, reactions)


@app.patch("/events/{event_id}")
async def patch_event(event_id: str, request: Request):
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    ttl = get_ttl()

    if not sid:
        res = Response(status_code=401)
        res.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return res

    key = redis_key(sid)
    session_data = redis_client.hgetall(key)

    if not session_data or "user_id" not in session_data:
        res = Response(status_code=401)
        res.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return res

    redis_client.expire(key, ttl)
    user_id = session_data["user_id"]

    try:
        oid = ObjectId(event_id)
    except Exception:
        res = JSONResponse(status_code=404, content={"message": "Not found. Be sure that event exists and you are the organizer"})
        res.set_cookie(key=SESSION_COOKIE_NAME, value=sid, httponly=True, max_age=ttl, path="/")
        return res

    event = events_collection.find_one({"_id": oid})
    if not event or event["created_by"] != user_id:
        res = JSONResponse(status_code=404, content={"message": "Not found. Be sure that event exists and you are the organizer"})
        res.set_cookie(key=SESSION_COOKIE_NAME, value=sid, httponly=True, max_age=ttl, path="/")
        return res

    body = await request.json()
    update_fields = {}

    category = body.get("category")
    price = body.get("price")
    city = body.get("city")

    if category is not None:
        valid_categories = ["meetup", "concert", "exhibition", "party", "other"]
        if category not in valid_categories:
            res = JSONResponse(status_code=400, content={"message": 'invalid "category" field'})
            res.set_cookie(key=SESSION_COOKIE_NAME, value=sid, httponly=True, max_age=ttl, path="/")
            return res
        update_fields["category"] = category

    if price is not None:
        if not isinstance(price, int) or price < 0:
            res = JSONResponse(status_code=400, content={"message": 'invalid "price" field'})
            res.set_cookie(key=SESSION_COOKIE_NAME, value=sid, httponly=True, max_age=ttl, path="/")
            return res
        update_fields["price"] = price

    if city is not None:
        if not isinstance(city, str):
            res = JSONResponse(status_code=400, content={"message": 'invalid "city" field'})
            res.set_cookie(key=SESSION_COOKIE_NAME, value=sid, httponly=True, max_age=ttl, path="/")
            return res
        if city == "":
            events_collection.update_one({"_id": oid}, {"$unset": {"location.city": ""}})
        else:
            update_fields["location.city"] = city

    if update_fields:
        set_fields = {}
        for k, v in update_fields.items():
            if k.startswith("location."):
                set_fields["location.city"] = v
            else:
                set_fields[k] = v
        events_collection.update_one({"_id": oid}, {"$set": set_fields})

    res = Response(status_code=204)
    res.set_cookie(key=SESSION_COOKIE_NAME, value=sid, httponly=True, max_age=ttl, path="/")
    return res

@app.get("/users")
def get_users(request: Request):
    limit = int(request.query_params.get("limit", 10))
    offset = int(request.query_params.get("offset", 0))
    name = request.query_params.get("name")
    user_id = request.query_params.get("id")

    query = {}
    if name:
        query["full_name"] = {"$regex": name, "$options": "i"}
    if user_id:
        try:
            query["_id"] = ObjectId(user_id)
        except Exception:
            return {"users": [], "count": 0}

    cursor = users_collection.find(query, {"password_hash": 0}).skip(offset).limit(limit)
    users = []
    for u in cursor:
        users.append({
            "id": str(u["_id"]),
            "full_name": u["full_name"],
            "username": u["username"],
        })

    return {
        "users": users,
        "count": len(users)
    }


@app.get("/users/{user_id}")
def get_user(user_id: str, request: Request):
    try:
        oid = ObjectId(user_id)
    except Exception:
        return JSONResponse(status_code=404, content={"message": "Not found"})

    user = users_collection.find_one({"_id": oid}, {"password_hash": 0})
    if not user:
        return JSONResponse(status_code=404, content={"message": "Not found"})

    return {
        "id": str(user["_id"]),
        "full_name": user["full_name"],
        "username": user["username"],
    }


@app.get("/users/{user_id}/events")
def get_user_events(user_id: str, request: Request):
    try:
        oid = ObjectId(user_id)
    except Exception:
        return JSONResponse(status_code=404, content={"message": "User not found"})

    user = users_collection.find_one({"_id": oid})
    if not user:
        return JSONResponse(status_code=404, content={"message": "User not found"})

    query_or_error, error_response = build_events_query(request)
    if error_response is not None:
        return error_response

    query_or_error["created_by"] = user_id

    events_cursor = events_collection.find(query_or_error)
    include_reactions = request.query_params.get("include") == "reactions"
    events = []
    for e in events_cursor:
        reactions = get_reactions_for_title(e["title"]) if include_reactions else None
        events.append(event_to_response(e, reactions))

    return {
        "events": events,
        "count": len(events)
    }

@app.post("/events/{event_id}/like")
async def like_event(event_id: str, request: Request, response: Response):
    sid, session_data = get_session_data(request)
    if not session_data or "user_id" not in session_data:
        res = Response(status_code=401)
        res.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return res

    try:
        oid = ObjectId(event_id)
    except Exception:
        res = JSONResponse(status_code=404, content={"message": "Event not found"})
        res.set_cookie(key=SESSION_COOKIE_NAME, value=sid, httponly=True, max_age=get_ttl(), path="/")
        return res

    event = events_collection.find_one({"_id": oid})
    if not event:
        res = JSONResponse(status_code=404, content={"message": "Event not found"})
        res.set_cookie(key=SESSION_COOKIE_NAME, value=sid, httponly=True, max_age=get_ttl(), path="/")
        return res

    s = get_cassandra()
    if not s:
        res = JSONResponse(status_code=503, content={"message": "Cassandra unavailable"})
        res.set_cookie(key=SESSION_COOKIE_NAME, value=sid, httponly=True, max_age=get_ttl(), path="/")
        return res

    user_id = session_data["user_id"]

    old_rows = s.execute(
        "SELECT like_value FROM event_reactions WHERE event_id=%s AND created_by=%s",
        (event_id, user_id)
    )
    old_value = old_rows[0]["like_value"] if old_rows else None

    s.execute(
        "INSERT INTO event_reactions (event_id, created_by, like_value, created_at) VALUES (%s, %s, %s, %s)",
        (event_id, user_id, 1, datetime.now(timezone.utc))
    )

    cache_key = f"event:{get_title_md5(event['title'])}:reactions"
    if not redis_client.exists(cache_key):
        redis_client.hset(cache_key, mapping={"likes": 0, "dislikes": 0})
    if old_value is None:
        redis_client.hincrby(cache_key, "likes", 1)
    elif old_value == -1:
        redis_client.hincrby(cache_key, "dislikes", -1)
        redis_client.hincrby(cache_key, "likes", 1)
    redis_client.expire(cache_key, int(os.getenv("APP_LIKE_TTL", "60")))

    def prolong_ttl():
        time.sleep(1)
        redis_client.expire(cache_key, 600)
    threading.Thread(target=prolong_ttl, daemon=True).start()

    redis_client.expire(redis_key(sid), get_ttl())
    res = Response(status_code=204)
    res.set_cookie(key=SESSION_COOKIE_NAME, value=sid, httponly=True, max_age=get_ttl(), path="/")
    return res


@app.post("/events/{event_id}/dislike")
async def dislike_event(event_id: str, request: Request, response: Response):
    sid, session_data = get_session_data(request)
    if not session_data or "user_id" not in session_data:
        res = Response(status_code=401)
        res.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return res

    try:
        oid = ObjectId(event_id)
    except Exception:
        res = JSONResponse(status_code=404, content={"message": "Event not found"})
        res.set_cookie(key=SESSION_COOKIE_NAME, value=sid, httponly=True, max_age=get_ttl(), path="/")
        return res

    event = events_collection.find_one({"_id": oid})
    if not event:
        res = JSONResponse(status_code=404, content={"message": "Event not found"})
        res.set_cookie(key=SESSION_COOKIE_NAME, value=sid, httponly=True, max_age=get_ttl(), path="/")
        return res

    s = get_cassandra()
    if not s:
        res = JSONResponse(status_code=503, content={"message": "Cassandra unavailable"})
        res.set_cookie(key=SESSION_COOKIE_NAME, value=sid, httponly=True, max_age=get_ttl(), path="/")
        return res

    user_id = session_data["user_id"]

    old_rows = s.execute(
        "SELECT like_value FROM event_reactions WHERE event_id=%s AND created_by=%s",
        (event_id, user_id)
    )
    old_value = old_rows[0]["like_value"] if old_rows else None

    s.execute(
        "INSERT INTO event_reactions (event_id, created_by, like_value, created_at) VALUES (%s, %s, %s, %s)",
        (event_id, user_id, -1, datetime.now(timezone.utc))
    )

    cache_key = f"event:{get_title_md5(event['title'])}:reactions"
    if not redis_client.exists(cache_key):
        redis_client.hset(cache_key, mapping={"likes": 0, "dislikes": 0})
    if old_value is None:
        redis_client.hincrby(cache_key, "dislikes", 1)
    elif old_value == 1:
        redis_client.hincrby(cache_key, "likes", -1)
        redis_client.hincrby(cache_key, "dislikes", 1)
    redis_client.expire(cache_key, int(os.getenv("APP_LIKE_TTL", "60")))

    def prolong_ttl():
        time.sleep(1)
        redis_client.expire(cache_key, 600)
    threading.Thread(target=prolong_ttl, daemon=True).start()

    redis_client.expire(redis_key(sid), get_ttl())
    res = Response(status_code=204)
    res.set_cookie(key=SESSION_COOKIE_NAME, value=sid, httponly=True, max_age=get_ttl(), path="/")
    return res

if __name__ == "__main__":
    raw_host = os.getenv("APP_HOST", "0.0.0.0")
    host = raw_host.replace("http://", "").replace("https://", "").strip("/")
    port = int(os.getenv("APP_PORT", "8080"))

    uvicorn.run(
        app,
        host=host,
        port=port,
    )
import os
import redis
import uvicorn
import secrets
import bcrypt

from bson import ObjectId
from pymongo import MongoClient, ASCENDING
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse


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


def event_to_response(e):
    location = e.get("location", {})
    return {
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


@app.get("/events")
def get_events(request: Request):
    title = request.query_params.get("title")
    limit = int(request.query_params.get("limit", 10))
    offset = int(request.query_params.get("offset", 0))
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
            return {"events": [], "count": 0}

    if category:
        valid_categories = ["meetup", "concert", "exhibition", "party", "other"]
        if category not in valid_categories:
            return JSONResponse(status_code=400, content={"message": 'invalid "category" field'})
        query["category"] = category

    if price_from is not None or price_to is not None:
        price_filter = {}
        if price_from is not None:
            try:
                p_from = int(price_from)
                price_filter["$gte"] = p_from
            except ValueError:
                return JSONResponse(status_code=400, content={"message": 'invalid "price_from" field'})
        if price_to is not None:
            try:
                p_to = int(price_to)
                price_filter["$lte"] = p_to
            except ValueError:
                return JSONResponse(status_code=400, content={"message": 'invalid "price_to" field'})
        query["price"] = price_filter

    if city:
        query["location.city"] = city

    if date_from:
        try:
            dt = datetime.strptime(date_from, "%Y%m%d")
            date_from_iso = dt.strftime("%Y-%m-%dT00:00:00Z")
            query["started_at"] = {**query.get("started_at", {}), "$gte": date_from_iso}
        except ValueError:
            return JSONResponse(status_code=400, content={"message": 'invalid "date_from" field'})

    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y%m%d")
            date_to_iso = dt.strftime("%Y-%m-%dT23:59:59Z")
            started_at_filter = query.get("started_at", {})
            started_at_filter["$lte"] = date_to_iso
            query["started_at"] = started_at_filter
        except ValueError:
            return JSONResponse(status_code=400, content={"message": 'invalid "date_to" field'})

    if user:
        found_user = users_collection.find_one({"username": user})
        if found_user:
            query["created_by"] = str(found_user["_id"])
        else:
            return {"events": [], "count": 0}

    cursor = events_collection.find(query).skip(offset).limit(limit)
    events = [event_to_response(e) for e in cursor]

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

    return event_to_response(e)


@app.patch("/events/{event_id}")
async def patch_event(event_id: str, request: Request, response: Response):
    sid, session = get_session_data(request)

    if not session or "user_id" not in session:
        return Response(status_code=401)

    try:
        oid = ObjectId(event_id)
    except Exception:
        return JSONResponse(status_code=404, content={"message": "Not found. Be sure that event exists and you are the organizer"})

    event = events_collection.find_one({"_id": oid})
    if not event or event["created_by"] != session["user_id"]:
        return JSONResponse(status_code=404, content={"message": "Not found. Be sure that event exists and you are the organizer"})

    body = await request.json()
    update_fields = {}

    category = body.get("category")
    price = body.get("price")
    city = body.get("city")

    if category is not None:
        valid_categories = ["meetup", "concert", "exhibition", "party", "other"]
        if category not in valid_categories:
            return JSONResponse(status_code=400, content={"message": 'invalid "category" field'})
        update_fields["category"] = category

    if price is not None:
        if not isinstance(price, int) or price < 0:
            return JSONResponse(status_code=400, content={"message": 'invalid "price" field'})
        update_fields["price"] = price

    if city is not None:
        if not isinstance(city, str):
            return JSONResponse(status_code=400, content={"message": 'invalid "city" field'})
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

    ttl = get_ttl()
    redis_client.expire(redis_key(sid), ttl)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=sid,
        httponly=True,
        max_age=ttl,
        path="/",
    )
    return Response(status_code=204)


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

    events_cursor = events_collection.find({"created_by": user_id})
    events = [event_to_response(e) for e in events_cursor]

    return {
        "events": events,
        "count": len(events)
    }

if __name__ == "__main__":
    raw_host = os.getenv("APP_HOST", "0.0.0.0")
    host = raw_host.replace("http://", "").replace("https://", "").strip("/")
    port = int(os.getenv("APP_PORT", "8080"))

    uvicorn.run(
        app,
        host=host,
        port=port,
    )
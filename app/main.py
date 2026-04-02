import os
import redis
import uvicorn
import secrets
import bcrypt

from pymongo import MongoClient
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

    return sid, data

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

        while True:
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
            samesite ="lax",
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
            samesite="lax",
        )

        return Response(status_code=200, headers=response.headers)

    while True:
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
        samesite="lax",
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
        samesite="lax",
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

    sid = request.cookies.get(SESSION_COOKIE_NAME)
    ttl = get_ttl()

    if not sid:
        sid = generate_sid()

    key = redis_key(sid)

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

    res = Response(status_code=204, content=b"")

    res.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=sid,
        httponly=True,
        max_age=ttl,
        path="/",
        samesite="lax",
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

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value="",
        httponly=True,
        max_age=0,
        path="/",
        samesite="lax",
    )

    return Response(status_code=204, headers=response.headers)

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

    if not finished_at or not isinstance(finished_at, str):
        return JSONResponse(status_code=400, content={"message": 'invalid "finished_at" field'})

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

    redis_client.expire(redis_key(sid), get_ttl())

    return JSONResponse(
        status_code=201,
        content={"id": str(result.inserted_id)}
    )

@app.get("/events")
def get_events(request: Request):

    title = request.query_params.get("title")
    limit = int(request.query_params.get("limit", 10))
    offset = int(request.query_params.get("offset", 0))

    query = {}

    if title:
        query["title"] = {"$regex": title, "$options": "i"}

    cursor = events_collection.find(query).skip(offset).limit(limit)

    events = []
    for e in cursor:
        events.append({
            "id": str(e["_id"]),
            "title": e["title"],
            "description": e.get("description"),
            "location": e.get("location"),
            "created_at": e.get("created_at"),
            "created_by": e.get("created_by"),
            "started_at": e.get("started_at"),
            "finished_at": e.get("finished_at"),
        })

    return {
        "events": events,
        "count": len(events)
    }

if __name__ == "__main__":

    uvicorn.run(
        app,
        host=os.getenv("APP_HOST"),
        port=int(os.getenv("APP_PORT")),
    )
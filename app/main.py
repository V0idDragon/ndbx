import os
import redis
import uvicorn
import secrets

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

SESSION_COOKIE_NAME = "X-Session-Id"

def generate_sid():
    return secrets.token_hex(16)

def get_ttl():
    return int(os.getenv("APP_USER_SESSION_TTL"))


def redis_key(sid: str):
    return f"sid:{sid}"


def now():
    return datetime.now(timezone.utc).isoformat()

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

    # NO COOKIE

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
        )

        response.status_code = 201
        return


    # COOKIE EXISTS


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

        response.status_code = 200
        return

    # COOKIE SESSION EXPIRED

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
    )

    response.status_code = 201

if __name__ == "__main__":

    uvicorn.run(
        app,
        host=os.getenv("APP_HOST"),
        port=int(os.getenv("APP_PORT")),
    )
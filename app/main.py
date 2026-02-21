import os
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI()

@app.get("/health")
def healthcheck():
    return JSONResponse(
        status_code=200,
        content={"status": "ok"}
    )

if __name__ == "__main__":
    import uvicorn

    # Read port from environment variable
    port = int(os.getenv("APP_PORT"))

    uvicorn.run(app, host="0.0.0.0", port=port)
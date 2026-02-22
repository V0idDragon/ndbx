import os
import uvicorn
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

    # Read port from environment variable
    port = int(os.getenv("APP_PORT"))
    host = os.getenv("APP_HOST")

    uvicorn.run(app, host=host, port=port)
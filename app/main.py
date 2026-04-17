"""NBA Prediction Market API.

    uvicorn app.main:app --reload
    # Docs: http://127.0.0.1:8000/docs
"""

from fastapi import FastAPI

from app.api.routes import analytics, candlesticks, events, markets

app = FastAPI(
    title="NBA Prediction Market API",
    description="Query Kalshi NBA game markets and OHLC candlestick data.",
    version="0.1.0",
)

app.include_router(markets.router)
app.include_router(events.router)
app.include_router(candlesticks.router)
app.include_router(analytics.router)
